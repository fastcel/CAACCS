#!/usr/bin/env python3
"""FYP network-only TLS 1.3 benchmarking pilot on WSL2.

Reads data/scenarios.csv and tests the seven scenarios that change ONLY
networking relative to S001: S001, S006, S007, S008, S009, S010, S011.

Network controls: netem per-interface delay, rate and random loss.
RTT / ping loss are observed for each scenario; rate is CONFIGURED, but
throughput is NOT retested by this script. The 100/25/5 Mbps levels were
validated separately with iperf3. CPU target and memory limit are NOT
implemented, regardless of the values carried over from scenarios.csv.

Before running: start the existing OpenSSL s_server in fyp-server namespace.
Run from anywhere: python3 network_conditions_pilot.py
Preview scenario plan without touching the network:
    python3 network_conditions_pilot.py --check

Needs: Linux 'tc', 'ping', 'sudo', /usr/bin/time, OpenSSL 3.5, server cert.
Only the two named FYP veth interfaces are reconfigured; existing root qdiscs
on those interfaces are removed on clean exit or Ctrl+C (not Ctrl+Z).
"""

import argparse
import csv
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import random
import re
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SCENARIOS_FILE = DATA_DIR / "scenarios.csv"
OPENSSL = "/opt/openssl-3.5/bin/openssl"
GNU_TIME = "/usr/bin/time"
CERT = Path.home() / "fyp-tls-test" / "server.crt"
TARGET_IP = "10.10.0.2"
TLS_ENDPOINT = f"{TARGET_IP}:4433"
PROFILES = ("X25519", "MLKEM768", "X25519MLKEM768")
NETWORK_SCENARIO_IDS = ("S001", "S006", "S007", "S008", "S009", "S010", "S011")
WARMUPS_PER_PROFILE = 1
RNG = random.Random(2026)

ENV = os.environ.copy()
ENV["LD_LIBRARY_PATH"] = "/opt/openssl-3.5/lib64:" + ENV.get("LD_LIBRARY_PATH", "")
ENV["LC_ALL"] = "C"

FIELDS = [
    "run_id", "scenario_id", "device_id", "profile", "repetition",
    "trial_order_in_scenario", "timestamp_utc",
    "cpu_load_target_pct", "memory_limit_mb",
    "cpu_control_applied", "memory_control_applied",
    "rtt_target_ms", "rtt_observed_ms",
    "validation_ping_packets_sent", "validation_ping_packets_received",
    "validation_ping_loss_pct", "bandwidth_target_mbps",
    "bandwidth_measurement_status", "packet_loss_target_pct",
    "netem_loss_per_direction_pct", "elapsed_ms", "cpu_time_ms",
    "peak_memory_mb", "bytes_read", "bytes_written", "negotiated_group",
    "success", "error_reason", "measurement_scope",
]


def run_checked(args, *, timeout=None):
    proc = subprocess.run(args, capture_output=True, text=True, env=ENV,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit={proc.returncode}): {' '.join(args)}\n"
            + (proc.stderr or proc.stdout)[-1200:]
        )
    return proc.stdout


def load_scenarios(requested_ids):
    if not SCENARIOS_FILE.is_file():
        raise FileNotFoundError(f"Cannot find: {SCENARIOS_FILE}")
    with SCENARIOS_FILE.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        required = {
            "scenario_id", "device_id", "cpu_load_target_pct", "memory_limit_mb",
            "rtt_target_ms", "bandwidth_target_mbps", "packet_loss_target_pct",
        }
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("scenarios.csv is missing required columns")
        items = list(reader)
    lookup = {}
    for row in items:
        if row["scenario_id"] in lookup:
            raise ValueError(f"Duplicate scenario ID: {row['scenario_id']}")
        lookup[row["scenario_id"]] = row
    missing = set(requested_ids) - set(lookup)
    if missing:
        raise ValueError(f"Missing scenarios: {', '.join(sorted(missing))}")
    if "S001" not in lookup:
        raise ValueError("Baseline S001 missing")
    baseline = lookup["S001"]
    selected = []
    for sid in requested_ids:
        row = lookup[sid]
        for key in ("device_id", "cpu_load_target_pct", "memory_limit_mb"):
            if row[key] != baseline[key]:
                raise ValueError(f"{sid}: {key} differs from S001; not network-only")
        if float(row["rtt_target_ms"]) <= 0:
            raise ValueError(f"{sid}: RTT must be positive")
        if float(row["bandwidth_target_mbps"]) <= 0:
            raise ValueError(f"{sid}: bandwidth must be positive")
        pct = float(row["packet_loss_target_pct"])
        if not 0 <= pct < 100:
            raise ValueError(f"{sid}: loss target must be in [0,100)")
        selected.append(row)
    return selected


def per_direction_loss(round_trip_loss_pct):
    # A ping request crosses the client egress; its reply crosses server egress.
    # For independent random loss at both ends: P(RTT lost) = 1 - (1 - p)^2.
    return 100.0 * (1.0 - math.sqrt(1.0 - round_trip_loss_pct / 100.0))


def tc_apply(row):
    sid = row["scenario_id"]
    delay = f"{float(row['rtt_target_ms']) / 2.0:g}ms"
    bandwidth = f"{float(row['bandwidth_target_mbps']):g}mbit"
    loss_pct = per_direction_loss(float(row["packet_loss_target_pct"]))
    loss = f"{loss_pct:.6f}%"
    settings = ["root", "netem", "delay", delay, "rate", bandwidth, "loss", loss]
    run_checked(["sudo", "-n", "tc", "qdisc", "replace", "dev", "veth-client", *settings])
    run_checked(["sudo", "-n", "ip", "netns", "exec", "fyp-server",
                 "tc", "qdisc", "replace", "dev", "veth-server", *settings])
    # Check both qdisc configurations; do not confuse 'requested' with 'observed'.
    for args in (
        ["tc", "qdisc", "show", "dev", "veth-client"],
        ["sudo", "-n", "ip", "netns", "exec", "fyp-server", "tc", "qdisc", "show", "dev", "veth-server"],
    ):
        out = run_checked(args)
        if "netem" not in out or not re.search(rf"\bdelay\s+{re.escape(delay)}\b", out) \
                or not re.search(rf"\brate\s+{float(row['bandwidth_target_mbps']):g}Mbit\b", out):
            raise RuntimeError(f"{sid}: unexpected tc settings: {out.strip()}")
        if loss_pct > 0 and "loss " not in out:
            raise RuntimeError(f"{sid}: nonzero netem loss setting missing: {out.strip()}")
    return round(loss_pct, 6)


def validate_ping(row):
    # Small pilot samples for RTT-only/bandwidth scenarios; enough packets to
    # observe low random loss on S010/S011, but never claim exact loss from them.
    desired = float(row["packet_loss_target_pct"])
    packets = 500 if desired > 0 else 10
    timeout_s = 85 if packets == 500 else 15
    command = ["ping", "-n", "-q", "-c", str(packets), "-i", "0.1", "-W", "1", TARGET_IP]
    res = subprocess.run(command, capture_output=True, text=True,
                         env=ENV, timeout=timeout_s)
    out = res.stdout + res.stderr
    counts = re.search(r"(\d+) packets transmitted,\s*(\d+) received,\s*([\d.]+)% packet loss", out)
    rtt = re.search(r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = [\d.]+/([\d.]+)/", out)
    if not counts or not rtt:
        raise RuntimeError(f"Unable to parse ping test for {row['scenario_id']}:\n{out}")
    observed_rtt = float(rtt.group(1))
    target = float(row["rtt_target_ms"])
    if abs(observed_rtt - target) > 5:
        raise RuntimeError(f"{row['scenario_id']}: observed RTT {observed_rtt} differs from target {target} by >5ms")
    return {
        "rtt_observed_ms": round(observed_rtt, 3),
        "validation_ping_packets_sent": int(counts.group(1)),
        "validation_ping_packets_received": int(counts.group(2)),
        "validation_ping_loss_pct": float(counts.group(3)),
    }


def bench_tls(profile):
    command = [GNU_TIME, "-f", "__MAX_MEMORY_KB__:%M", OPENSSL, "s_client",
               "-connect", TLS_ENDPOINT, "-tls1_3", "-groups", profile,
               "-ciphersuites", "TLS_AES_128_GCM_SHA256", "-CAfile", str(CERT),
               "-verify_ip", TARGET_IP, "-verify_return_error"]
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    stdout = ""
    stderr = ""
    returncode = None
    error_reason = ""
    try:
        completed = subprocess.run(command, input="\n", capture_output=True, text=True,
                                   env=ENV, timeout=20)
        stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as ex:
        stdout = ex.stdout.decode(errors="replace") if isinstance(ex.stdout, bytes) else (ex.stdout or "")
        stderr = ex.stderr.decode(errors="replace") if isinstance(ex.stderr, bytes) else (ex.stderr or "")
        error_reason = "timeout"
    elapsed_ms = (time.perf_counter() - t0) * 1000
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_ms = ((cpu_after.ru_utime + cpu_after.ru_stime) -
              (cpu_before.ru_utime + cpu_before.ru_stime)) * 1000
    full = stdout + stderr
    sizes = re.search(r"SSL handshake has read (\d+) bytes and written (\d+) bytes", full)
    rss = re.search(r"__MAX_MEMORY_KB__:(\d+)", stderr)
    group_matches = re.findall(
        r"(?:Server Temp Key|Peer Temp Key|Negotiated TLS1\.3 group):\s*([A-Za-z0-9]+)", full
    )
    negotiated = group_matches[-1] if group_matches else ""
    verified = "Verification: OK" in full or "Verify return code: 0 (ok)" in full
    success = returncode == 0 and "TLSv1.3" in full and verified and negotiated == profile
    if not success and not error_reason:
        error_reason = f"failed verification/protocol/group (exit={returncode}): " + stderr[-200:].replace("\n", " ")
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
    # Deliberately limited to the two FYP virtual interfaces.
    commands = (
        ["sudo", "-n", "tc", "qdisc", "del", "dev", "veth-client", "root"],
        ["sudo", "-n", "ip", "netns", "exec", "fyp-server",
         "tc", "qdisc", "del", "dev", "veth-server", "root"],
    )
    for cmd in commands:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
        if proc.returncode != 0:
            print("Warning: could not clean up", " ".join(cmd), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Only display the chosen scenarios")
    parser.add_argument("--repetitions", type=int, default=3,
                        help="Measured repetitions per profile per scenario (default 3)")
    parser.add_argument("--scenarios", nargs="+", choices=NETWORK_SCENARIO_IDS,
                        help="Run only specified network scenario IDs")
    args = parser.parse_args()
    if args.repetitions <= 0:
        parser.error("--repetitions must be at least 1")
    ids = args.scenarios or NETWORK_SCENARIO_IDS
    scenarios = load_scenarios(ids)
    print("Network-only pilot: CPU load and memory limits are NOT applied.")
    print("Bandwidth rate is configured with netem; iperf3 throughput is NOT retested in this run.")
    print("S002-S005 (CPU/memory variations) are intentionally excluded.")
    print(f"Selected: {', '.join(ids)}; repetitions/profile={args.repetitions}")
    for row in scenarios:
        print(f"  {row['scenario_id']}: RTT={row['rtt_target_ms']}ms, "
              f"BW={row['bandwidth_target_mbps']}Mbps, round-trip loss={row['packet_loss_target_pct']}%")
    if args.check:
        return

    for p in (Path(OPENSSL), Path(GNU_TIME), CERT):
        if not p.is_file():
            raise FileNotFoundError(f"Missing required file: {p}")
    print("\nPreflight: checking OpenSSL TLS server and certificate...")
    test = bench_tls("X25519")
    if not test["success"]:
        raise RuntimeError("TLS preflight failed. Start the TLS server in fyp-server namespace, "
                           "then rerun. Detail: " + test["error_reason"])
    print("TLS preflight successful.")
    run_checked(["sudo", "-v"])
    output_name = "raw_network_conditions_pilot_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".csv"
    DATA_DIR.mkdir(exist_ok=True)
    output_path = DATA_DIR / output_name
    run_index = 0
    ok_count = 0
    try:
        with output_path.open("x", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            fh.flush()
            for row in scenarios:
                sid = row["scenario_id"]
                print(f"\nApplying {sid}...", flush=True)
                applied_loss = tc_apply(row)
                observed = validate_ping(row)
                print(f"Ping: RTT={observed['rtt_observed_ms']} ms; "
                      f"loss={observed['validation_ping_loss_pct']}% "
                      f"({observed['validation_ping_packets_sent']} pings); "
                      f"one-way netem loss={applied_loss}%", flush=True)
                for profile in PROFILES:
                    bench_tls(profile)  # discard 1 warm-up per profile
                trials = [(profile, rep) for profile in PROFILES
                          for rep in range(1, args.repetitions + 1)]
                RNG.shuffle(trials)
                for order, (profile, rep) in enumerate(trials, start=1):
                    measurement = bench_tls(profile)
                    run_index += 1
                    ok_count += int(measurement["success"])
                    writer.writerow({
                        "run_id": f"R{run_index:05d}",
                        "scenario_id": sid,
                        "device_id": row["device_id"],
                        "profile": profile,
                        "repetition": rep,
                        "trial_order_in_scenario": order,
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "cpu_load_target_pct": row["cpu_load_target_pct"],
                        "memory_limit_mb": row["memory_limit_mb"],
                        "cpu_control_applied": False,
                        "memory_control_applied": False,
                        **observed,
                        "rtt_target_ms": row["rtt_target_ms"],
                        "bandwidth_target_mbps": row["bandwidth_target_mbps"],
                        "bandwidth_measurement_status": "configured_not_measured_this_run",
                        "packet_loss_target_pct": row["packet_loss_target_pct"],
                        "netem_loss_per_direction_pct": applied_loss,
                        "measurement_scope": "network_only_pilot",
                        **measurement,
                    })
                    fh.flush()  # preserve completed trials even if stopped later
                    print(f"{sid} | {profile} | rep {rep} | "
                          f"{measurement['elapsed_ms']} ms | success={measurement['success']}",
                          flush=True)
                    if not measurement["success"]:
                        print("  Problem:", measurement["error_reason"], flush=True)
        print(f"\nFinished: {ok_count}/{run_index} TLS runs reported successful.")
        print("Results saved to:", output_path)
    finally:
        cleanup_tc()
        print("Removed temporary traffic shaping from both FYP interfaces.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted with Ctrl+C. Previous CSV rows were preserved.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
