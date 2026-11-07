#!/usr/bin/env python3
"""Network-only TLS RTT pilot for the FYP, using OpenSSL 3.5 on WSL2.

Reads data/scenarios.csv, selecting S001/S006/S007, and automatically applies
RTT shaping to the existing veth-client/fyp-server:veth-server test network.
It does NOT control CPU load, memory pressure/limits, or validate actual link
throughput. Keep these runs separate from fully controlled FYP results.

Run from the project's directory with the TLS server already listening:
    python3 network_rtt_pilot.py
Requires sudo rights for tc; only the two FYP virtual interfaces are modified.
"""

import csv
import os
import random
import re
import resource
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCENARIOS_FILE = ROOT / "data" / "scenarios.csv"
DATA_DIR = ROOT / "data"
OPENSSL = "/opt/openssl-3.5/bin/openssl"
GNU_TIME = "/usr/bin/time"
CERTIFICATE = Path.home() / "fyp-tls-test" / "server.crt"
TARGET_IP = "10.10.0.2"
SERVER = f"{TARGET_IP}:4433"
PROFILES = ("X25519", "MLKEM768", "X25519MLKEM768")
PILOT_SCENARIOS = ("S001", "S006", "S007")
REPETITIONS = 3  # Pilot only; increase after inspecting variance.
WARMUPS_PER_PROFILE = 1
RNG = random.Random(2026)

ENV = os.environ.copy()
ENV["LD_LIBRARY_PATH"] = "/opt/openssl-3.5/lib64:" + ENV.get("LD_LIBRARY_PATH", "")
ENV["LC_ALL"] = "C"

FIELDS = [
    "run_id", "scenario_id", "device_id", "profile", "repetition",
    "trial_order_in_scenario", "timestamp_utc", "rtt_target_ms", "rtt_observed_ms",
    "validation_ping_loss_pct", "bandwidth_target_mbps", "packet_loss_target_pct",
    "elapsed_ms", "cpu_time_ms", "peak_memory_mb", "bytes_read", "bytes_written",
    "negotiated_group", "success", "error_reason", "measurement_scope",
]


def run_cmd(argv, *, capture=False):
    return subprocess.run(argv, check=True, text=True, capture_output=capture, env=ENV)


def load_scenarios():
    if not SCENARIOS_FILE.is_file():
        raise FileNotFoundError(f"Missing {SCENARIOS_FILE}. Create scenarios.csv first.")
    with SCENARIOS_FILE.open(newline="") as f:
        by_id = {row["scenario_id"]: row for row in csv.DictReader(f)}
    selected = []
    for scenario_id in PILOT_SCENARIOS:
        row = by_id[scenario_id]
        bw = float(row["bandwidth_target_mbps"])
        loss = float(row["packet_loss_target_pct"])
        if bw != 100 or loss != 0:
            raise ValueError(f"{scenario_id}: this pilot expects 100 Mbps and 0% induced loss")
        selected.append(row)
    return selected


def tc_apply(row):
    # 2 x one-way delay ≈ target RTT, with some processing overhead.
    half_rtt = float(row["rtt_target_ms"]) / 2.0
    delay = f"{half_rtt:g}ms"
    rate = f"{float(row['bandwidth_target_mbps']):g}mbit"
    loss = f"{float(row['packet_loss_target_pct']):g}%"
    run_cmd(["sudo", "tc", "qdisc", "replace", "dev", "veth-client",
             "root", "netem", "delay", delay, "rate", rate, "loss", loss])
    run_cmd(["sudo", "ip", "netns", "exec", "fyp-server", "tc", "qdisc",
             "replace", "dev", "veth-server", "root", "netem",
             "delay", delay, "rate", rate, "loss", loss])
    # Confirm both directions contain the requested netem settings.
    for argv in (["tc", "qdisc", "show", "dev", "veth-client"],
                 ["sudo", "ip", "netns", "exec", "fyp-server", "tc", "qdisc",
                  "show", "dev", "veth-server"]):
        out = run_cmd(argv, capture=True).stdout
        if "netem" not in out or f"delay {delay}" not in out or "rate 100Mbit" not in out:
            raise RuntimeError(f"Traffic control did not match requested settings: {out.strip()}")


def validate_rtt(target_ms):
    output = run_cmd(["ping", "-n", "-c", "10", "-W", "2", TARGET_IP], capture=True).stdout
    rtt = re.search(r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = [\d.]+/([\d.]+)/", output)
    loss = re.search(r"([\d.]+)% packet loss", output)
    if not rtt or not loss:
        raise RuntimeError("Unable to parse ping summary:\n" + output)
    observed = float(rtt.group(1))
    if abs(observed - target_ms) > 5:
        raise RuntimeError(f"RTT not within ±5 ms of target: target={target_ms}, observed={observed}")
    return observed, float(loss.group(1))


def benchmark(profile):
    cmd = [GNU_TIME, "-f", "__MAX_MEMORY_KB__:%M", OPENSSL, "s_client",
           "-connect", SERVER, "-tls1_3", "-groups", profile,
           "-ciphersuites", "TLS_AES_128_GCM_SHA256",
           "-CAfile", str(CERTIFICATE), "-verify_ip", TARGET_IP,
           "-verify_return_error"]
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    start = time.perf_counter()
    stdout, stderr, returncode, error_reason = "", "", None, ""
    try:
        result = subprocess.run(cmd, input="\n", text=True, capture_output=True,
                                env=ENV, timeout=12)
        stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        error_reason = "timeout"
    elapsed_ms = (time.perf_counter() - start) * 1000
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_ms = ((cpu_after.ru_utime + cpu_after.ru_stime) -
              (cpu_before.ru_utime + cpu_before.ru_stime)) * 1000

    full = stdout + stderr
    sizes = re.search(r"SSL handshake has read (\d+) bytes and written (\d+) bytes", full)
    rss = re.search(r"__MAX_MEMORY_KB__:(\d+)", stderr)
    keys = re.findall(r"(?:Server Temp Key|Peer Temp Key|Negotiated TLS1\.3 group):\s*([A-Za-z0-9]+)", full)
    negotiated = keys[-1] if keys else ""
    verified = "Verification: OK" in full or "Verify return code: 0 (ok)" in full
    success = returncode == 0 and "TLSv1.3" in full and verified and negotiated == profile
    if not success and not error_reason:
        error_reason = f"TLS verification/group/exit failed (code={returncode}); " + stderr[-180:].replace("\n", " ")

    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "cpu_time_ms": round(cpu_ms, 3),
        "peak_memory_mb": round(int(rss.group(1)) / 1024, 3) if rss else "",
        "bytes_read": int(sizes.group(1)) if sizes else "",
        "bytes_written": int(sizes.group(2)) if sizes else "",
        "negotiated_group": negotiated,
        "success": success,
        "error_reason": error_reason,
    }


def cleanup_tc():
    # Do not touch Windows or the physical network. Clear only the FYP veth qdiscs.
    for cmd in (["sudo", "tc", "qdisc", "del", "dev", "veth-client", "root"],
                ["sudo", "ip", "netns", "exec", "fyp-server", "tc", "qdisc",
                 "del", "dev", "veth-server", "root"]):
        subprocess.run(cmd, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    for required in (Path(OPENSSL), Path(GNU_TIME), CERTIFICATE):
        if not required.is_file():
            raise FileNotFoundError(f"Missing required file: {required}")
    scenarios = load_scenarios()
    DATA_DIR.mkdir(exist_ok=True)
    filename = "raw_network_rtt_automated_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
    path = DATA_DIR / filename
    print("This is a NETWORK-ONLY pilot: CPU load and memory restrictions are NOT applied.")
    print("Confirm the TLS server is running in the fyp-server namespace.")
    run_cmd(["sudo", "-v"])  # Prompt before measurements, not during individual trials.
    run_number = 1
    try:
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            for row in scenarios:
                sid = row["scenario_id"]
                print(f"\nApplying {sid}: RTT target={row['rtt_target_ms']} ms")
                tc_apply(row)
                observed_rtt, ping_loss = validate_rtt(float(row["rtt_target_ms"]))
                print(f"Verified average RTT={observed_rtt:.3f} ms; validation ping loss={ping_loss:g}%")
                for profile in PROFILES:
                    for _ in range(WARMUPS_PER_PROFILE):
                        benchmark(profile)  # Discard warm-up measurement.
                trials = [(p, rep) for p in PROFILES for rep in range(1, REPETITIONS + 1)]
                RNG.shuffle(trials)
                for trial_index, (profile, repetition) in enumerate(trials, 1):
                    measured = benchmark(profile)
                    record = {
                        "run_id": f"R{run_number:04d}",
                        "scenario_id": sid,
                        "device_id": row["device_id"],
                        "profile": profile,
                        "repetition": repetition,
                        "trial_order_in_scenario": trial_index,
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "rtt_target_ms": row["rtt_target_ms"],
                        "rtt_observed_ms": observed_rtt,
                        "validation_ping_loss_pct": ping_loss,
                        "bandwidth_target_mbps": row["bandwidth_target_mbps"],
                        "packet_loss_target_pct": row["packet_loss_target_pct"],
                        "measurement_scope": "network_only_pilot",
                        **measured,
                    }
                    writer.writerow(record)
                    f.flush()
                    print(f"{sid} | {profile} | rep {repetition} | {measured['elapsed_ms']} ms | success={measured['success']}")
                    if not measured["success"]:
                        print("  Issue:", measured["error_reason"])
                    run_number += 1
        print("\nFinished. Pilot results:", path)
    finally:
        cleanup_tc()
        print("Removed temporary traffic shaping from the two FYP virtual interfaces.")


if __name__ == "__main__":
    main()
