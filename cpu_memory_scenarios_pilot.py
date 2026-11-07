#!/usr/bin/env python3
"""Configurable CPU contention + cgroup memory-ceiling TLS 1.3 pilot for FYP.

Run next to network_conditions_pilot.py and data/scenarios.csv.
Preview: python3 cpu_memory_scenarios_pilot.py --scenario S003 --check
Run in a verified memory-limited systemd scope with normal user groups preserved:
  sudo systemd-run --scope --same-dir -p MemoryMax=4096M \
      /usr/sbin/runuser -u "$(id -un)" -- /bin/sh -c \
      'sudo -v && exec /usr/bin/python3 cpu_memory_scenarios_pilot.py --scenario S003'

This is a *host CPU contention* pilot. The TLS server shares WSL's CPUs with
client/stress workers, and MemoryMax is a ceiling, not a low-memory pressure
simulation. The measured elapsed time includes process startup and TCP/TLS.
Does not change netem settings or touch any previous CSVs.
"""

import argparse
import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import pwd
import random
import re
import signal
import subprocess
import sys
import threading
import time

import network_conditions_pilot as network

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
# CPU_REQUEST_PCT is an empirically calibrated setting for this WSL host, NOT
# the verified CPU utilization of any individual TLS trial.
SCENARIO_CPU_REQUEST = {
    "S002": 50,   # target 50%; observed 49.47% in initial TLS pilot
    "S003": 90,   # target 80%; observed 80.70% in calibration
    "S004": 20,   # target 20%; observed 18.10% in calibration
    "S005": 20,   # target 20%; observed 18.10% in calibration
}
PROFILES = network.PROFILES
CPU_METHOD = "matrixprod"
WARMUP_SEC = 7
GAP_BETWEEN_TRIALS_SEC = 1.0

# A scope started through `sudo systemd-run --uid=...` may inherit root's HOME.
# Select the TLS certificate based on the *effective user's account* instead.
USER_HOME = Path(pwd.getpwuid(os.geteuid()).pw_dir)
network.CERT = USER_HOME / "fyp-tls-test" / "server.crt"
network.ENV["HOME"] = str(USER_HOME)

FIELDS = [
    "run_id", "scenario_id", "device_id", "profile", "repetition",
    "trial_order_in_scenario", "timestamp_utc", "cpu_load_target_pct",
    "cpu_stress_requested_pct", "cpu_method", "cpu_workers",
    "cpu_observed_during_trial_pct_approx", "cpu_observed_over_benchmark_pct",
    "cpu_monitor_samples", "memory_limit_mb", "memory_control_applied",
    "memory_max_verified_bytes", "cgroup_memory_peak_mb",
    "rtt_target_ms", "rtt_observed_ms", "validation_ping_loss_pct",
    "bandwidth_target_mbps", "packet_loss_target_pct",
    "elapsed_ms", "cpu_time_ms", "peak_memory_mb",
    "bytes_read", "bytes_written", "negotiated_group",
    "success", "error_reason", "measurement_scope",
]


def read_scenario(scenario_id):
    with network.SCENARIOS_FILE.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    matches = [r for r in rows if r["scenario_id"] == scenario_id]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {scenario_id} in {network.SCENARIOS_FILE}")
    row = matches[0]
    expected_cpu = {"S002": 50, "S003": 80, "S004": 20, "S005": 20}[scenario_id]
    expected_memory = {"S002": 4096, "S003": 4096, "S004": 2048, "S005": 1024}[scenario_id]
    if (float(row["cpu_load_target_pct"]) != expected_cpu or
            float(row["memory_limit_mb"]) != expected_memory or
            float(row["rtt_target_ms"]) != 10 or
            float(row["bandwidth_target_mbps"]) != 100 or
            float(row["packet_loss_target_pct"]) != 0):
        raise RuntimeError(f"Unexpected {scenario_id} settings: {row}")
    return row


def get_cgroup_path():
    entry = next((line for line in Path("/proc/self/cgroup").read_text().splitlines()
                  if line.startswith("0::")), None)
    if entry is None:
        raise RuntimeError("Not running under a cgroup v2 hierarchy")
    relative = entry.partition("::")[2].lstrip("/")
    return Path("/sys/fs/cgroup") / relative


def validate_memory_scope(scenario_id, expected_bytes, memory_mib):
    cg = get_cgroup_path()
    mem_file = cg / "memory.max"
    observed = mem_file.read_text().strip()
    if observed != str(expected_bytes):
        raise RuntimeError(
            f"{scenario_id} requires a {memory_mib} MiB cgroup limit; "
            f"{mem_file} reads {observed!r}, expected {expected_bytes}. "
            "Launch via systemd-run --scope -p MemoryMax=<scenario memory>M "
            "and runuser, as described in the script docstring."
        )
    return cg


def read_cpu_ticks():
    # First eight /proc/stat fields; do NOT double count guest/guest_nice.
    with Path("/proc/stat").open() as fh:
        values = [int(x) for x in fh.readline().split()[1:9]]
    return sum(values), values[3]


def busy_pct(before, after):
    elapsed_total = after[0] - before[0]
    elapsed_idle = after[1] - before[1]
    if elapsed_total <= 0:
        return None
    return round(100 * (1 - elapsed_idle / elapsed_total), 2)


class CpuMonitor:
    def __init__(self):
        self.stop_event = threading.Event()
        self.samples = []
        self.thread = threading.Thread(target=self._collect, daemon=True)

    def _collect(self):
        previous = read_cpu_ticks()
        while not self.stop_event.wait(1.0):
            current = read_cpu_ticks()
            pct = busy_pct(previous, current)
            if pct is not None:
                self.samples.append({
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "cpu_utilization_pct": pct,
                })
            previous = current

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)


def check_network_settings(row, scenario_id):
    # This pilot does NOT change qdiscs, so insist on the manually verified S002 settings.
    args_list = [
        ["tc", "qdisc", "show", "dev", "veth-client"],
        ["sudo", "-n", "ip", "netns", "exec", "fyp-server",
         "tc", "qdisc", "show", "dev", "veth-server"],
    ]
    for args in args_list:
        result = network.run_checked(args)
        if not ("netem" in result and re.search(r"\bdelay\s+5ms\b", result)
                and re.search(r"\brate\s+100Mbit\b", result)):
            raise RuntimeError(f"{scenario_id} network configuration is missing: " + result.strip())
        match = re.search(r"\bloss\s+([0-9.]+)%", result)
        if match and float(match.group(1)) != 0.0:
            raise RuntimeError(f"{scenario_id} needs 0% configured packet loss: " + result.strip())
    metrics = network.validate_ping(row)
    if metrics["validation_ping_loss_pct"] != 0.0:
        raise RuntimeError(f"{scenario_id} validation pings must show 0% loss, retry network preflight")
    return metrics


def kill_stress(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=4)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Show settings without modifying anything")
    parser.add_argument("--scenario", choices=tuple(SCENARIO_CPU_REQUEST),
                        required=True, help="Select one CPU/memory scenario")
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be >= 1")
    scenario_id = args.scenario
    row = read_scenario(scenario_id)
    cpu_request_pct = SCENARIO_CPU_REQUEST[scenario_id]
    memory_mib = int(row["memory_limit_mb"])
    expected_mem_bytes = memory_mib * 1024 * 1024
    worker_count = len(os.sched_getaffinity(0))
    print(f"{scenario_id}: CPU target={row['cpu_load_target_pct']}%, "
          f"memory ceiling={row['memory_limit_mb']} MiB, RTT={row['rtt_target_ms']} ms, "
          f"bandwidth={row['bandwidth_target_mbps']} Mbps, loss={row['packet_loss_target_pct']}%")
    print(f"CPU stress: {worker_count} workers, method={CPU_METHOD}, requested={cpu_request_pct}%")
    print(f"Profiles: {', '.join(PROFILES)}; measured repetitions/profile={args.repetitions}")
    print("Scope: CPU contention on shared WSL host; memory ceiling, not simulated RAM pressure.")
    print("CPU per-trial ~ estimates are noisy over millisecond windows; use sampled aggregate with caution.")
    if args.check:
        print("Preview only. No processes were started and no files were changed.")
        return
    if os.geteuid() == 0:
        raise RuntimeError("Run as your normal Linux user via systemd-run + runuser, not root")
    for binary in ("/usr/bin/stress-ng", network.OPENSSL, network.GNU_TIME):
        if not Path(binary).exists():
            raise RuntimeError(f"Missing dependency: {binary}")
    if not network.CERT.is_file():
        raise RuntimeError(f"Missing certificate: {network.CERT}")
    cg = validate_memory_scope(scenario_id, expected_mem_bytes, memory_mib)
    print(f"Cgroup memory.max verified: {expected_mem_bytes} bytes ({cg})")
    # sudo may ask for password only if authentication has expired;
    # `sudo -n` in network checks intentionally fails rather than hanging.
    observed_network = check_network_settings(row, scenario_id)
    print(f"Network verified: avg RTT={observed_network['rtt_observed_ms']} ms; "
          f"ping loss={observed_network['validation_ping_loss_pct']}%")
    if not network.bench_tls("X25519")["success"]:
        raise RuntimeError("TLS preflight failed; check the OpenSSL server in fyp-server")
    print("Authenticated TLS preflight passed.")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    DATA_DIR.mkdir(exist_ok=True)
    csv_path = DATA_DIR / f"raw_cpu_memory_pilot_{scenario_id}_{timestamp}.csv"
    cpu_samples_path = DATA_DIR / f"cpu_samples_{scenario_id}_{timestamp}.csv"
    stress = None
    monitor = None
    measurements = []
    try:
        stress = subprocess.Popen(
            ["/usr/bin/stress-ng", "--cpu", str(worker_count),
             "--cpu-method", CPU_METHOD, "--cpu-load", str(cpu_request_pct),
             "--timeout", "120s"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        print(f"Starting CPU stress (PID={stress.pid}); stabilizing for {WARMUP_SEC} seconds...")
        time.sleep(WARMUP_SEC)
        if stress.poll() is not None:
            raise RuntimeError("CPU stress process exited prematurely")
        monitor = CpuMonitor()
        monitor.start()
        for profile in PROFILES:
            if not network.bench_tls(profile)["success"]:
                raise RuntimeError(f"Warm-up TLS handshake failed for {profile}")
        trials = [(p, i) for p in PROFILES for i in range(1, args.repetitions + 1)]
        random.Random(2026 + int(scenario_id[-3:])).shuffle(trials)
        for index, (profile, rep) in enumerate(trials, 1):
            if stress.poll() is not None:
                raise RuntimeError("CPU stress ended before TLS trials completed")
            before = read_cpu_ticks()
            measure = network.bench_tls(profile)
            after = read_cpu_ticks()
            measurements.append({
                "run_id": f"R{index:05d}", "scenario_id": scenario_id,
                "device_id": row["device_id"], "profile": profile,
                "repetition": rep, "trial_order_in_scenario": index,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "cpu_load_target_pct": row["cpu_load_target_pct"],
                "cpu_stress_requested_pct": cpu_request_pct,
                "cpu_method": CPU_METHOD, "cpu_workers": worker_count,
                "cpu_observed_during_trial_pct_approx": busy_pct(before, after),
                "memory_limit_mb": row["memory_limit_mb"],
                "memory_control_applied": True,
                "memory_max_verified_bytes": expected_mem_bytes,
                "rtt_target_ms": row["rtt_target_ms"],
                "rtt_observed_ms": observed_network["rtt_observed_ms"],
                "validation_ping_loss_pct": observed_network["validation_ping_loss_pct"],
                "bandwidth_target_mbps": row["bandwidth_target_mbps"],
                "packet_loss_target_pct": row["packet_loss_target_pct"],
                "measurement_scope": "shared_host_cpu_contention_memory_ceiling_pilot",
                **measure,
            })
            print(f"{scenario_id} | {profile} | rep {rep} | {measure['elapsed_ms']} ms | "
                  f"CPU during trial~{busy_pct(before, after)}% | "
                  f"success={measure['success']}", flush=True)
            if index < len(trials):
                time.sleep(GAP_BETWEEN_TRIALS_SEC)
    finally:
        if monitor:
            monitor.stop()
        kill_stress(stress)
        if monitor:
            with cpu_samples_path.open("x", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["timestamp_utc", "cpu_utilization_pct"])
                writer.writeheader()
                writer.writerows(monitor.samples)
        overall_cpu_pct = None
        if monitor and monitor.samples:
            overall_cpu_pct = round(sum(x["cpu_utilization_pct"] for x in monitor.samples)
                                    / len(monitor.samples), 2)
        peak_file = cg / "memory.peak"
        peak_mb = round(int(peak_file.read_text()) / (1024 ** 2), 3) if peak_file.is_file() else ""
        if measurements:
            with csv_path.open("x", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDS)
                writer.writeheader()
                for rec in measurements:
                    rec["cpu_observed_over_benchmark_pct"] = overall_cpu_pct
                    rec["cpu_monitor_samples"] = len(monitor.samples) if monitor else 0
                    rec["cgroup_memory_peak_mb"] = peak_mb
                    writer.writerow(rec)
            print(f"Saved {len(measurements)} TLS measurements: {csv_path}")
            print(f"CPU monitoring samples: {cpu_samples_path}")
            print(f"Observed average CPU utilization during benchmark: {overall_cpu_pct}%")
            print(f"Cgroup peak memory (whole scope): {peak_mb} MiB")
            print(f"Success count: {sum(bool(x['success']) for x in measurements)}/{len(measurements)}")
        print("CPU stress stopped; existing network shaping was NOT changed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted (Ctrl+C). See any partial CSV reported above.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
