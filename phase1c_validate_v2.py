#!/usr/bin/env python3
"""Read-only Phase 1C dataset validation, including TLS failures, for CAACCS.

Usage from the FYP repository root:
  python3 phase1c_validate_v2.py --scenario C034
  python3 phase1c_validate_v2.py --all

This checks selected full-batch CSVs, matching metadata/CPU samples, and the
frozen scenario plan. Failed TLS attempts remain in the 60-trial denominator;
their error reasons are required, and successful-only medians are reported.
A structurally valid batch with failed TLS attempts is VALID DATA, not 100%
TLS reliability. It does not merge data, change experiments, or mark held-out
scenarios as development data. Missing scenarios are reported as PENDING in --all.

Limits: the runner configures bandwidth but does not measure throughput. A
cgroup memory ceiling is not real RAM pressure. TLS handshake time measured
with SSL_connect includes network, crypto, verification and scheduling work.
"""

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean, median
import re
import sys

ROOT = Path(__file__).resolve().parent
PLAN = ROOT / "data" / "phase1c_experiment_plan.csv"
RAW = ROOT / "data" / "phase1c_raw"
GROUPS = ("X25519", "MLKEM768", "X25519MLKEM768")
METHOD = "openssl_ssl_connect_probe_v1"
SCOPE = "shared_host_cpu_contention_plus_netem_memory_ceiling"
FILE_RE = re.compile(r"^phase1c_planned_probe_(C\d{3})_\d{8}_\d{6}_\d{6}\.csv$")
NEEDED = {
    "scenario_id", "device_id", "profile", "repetition", "trial_order_in_scenario",
    "run_type", "dataset_split", "success", "error_reason", "negotiated_group", "measurement_method",
    "measurement_scope", "tcp_ms", "tls_handshake_ms", "elapsed_ms",
    "cpu_load_target_pct", "cpu_stress_requested_pct", "cpu_control_applied",
    "memory_limit_mb", "memory_control_applied", "memory_max_verified_bytes",
    "rtt_target_ms", "rtt_observed_ms", "bandwidth_target_mbps",
    "packet_loss_target_pct", "validation_ping_packets_sent",
    "validation_ping_packets_received", "validation_ping_loss_pct",
    "bandwidth_measurement_status", "netem_loss_per_direction_pct",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: invalid number {value!r}") from None
    require(math.isfinite(result), f"{label}: non-finite number")
    return result


def same_number(got, expected, label, tolerance=0.000001):
    a, b = number(got, label), number(expected, label + " in plan")
    require(abs(a-b) <= tolerance, f"{label}: observed {a}, planned {b}")


def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        require(len(fields) == len(set(fields)), f"{path.name}: duplicate CSV field name")
        rows = list(reader)
    require(bool(rows), f"{path.name}: empty CSV")
    require(all(None not in row and all(x is not None for x in row.values()) for row in rows),
            f"{path.name}: malformed CSV row")
    return set(fields), rows


def load_plan():
    fields, rows = read_csv(PLAN)
    required = {"scenario_id", "device_id", "cpu_load_target_pct",
                "cpu_stress_request_pct", "memory_ceiling_mib", "rtt_target_ms",
                "bandwidth_target_mbps", "packet_loss_target_pct",
                "repetitions_per_profile", "dataset_split", "execution_order"}
    require(required <= fields, "Plan is missing required fields")
    by_id = {}
    for row in rows:
        sid = row["scenario_id"]
        require(sid not in by_id, f"Duplicate plan scenario {sid}")
        by_id[sid] = row
    return by_id


def full_csv_files():
    out = {}
    if not RAW.exists():
        return out
    for f in RAW.iterdir():
        m = FILE_RE.fullmatch(f.name)
        if m:
            out.setdefault(m.group(1), []).append(f)
    return out


def validate_one(sid, paths, planned):
    require(len(paths) == 1,
            f"{sid}: {len(paths)} full-batch CSVs found; inspect duplicates manually, "
            "do not silently count multiple versions")
    path = paths[0]
    stem = path.stem
    meta_path = path.with_name(stem + "_metadata.json")
    samples_path = path.with_name(stem + "_cpu_samples.csv")
    require(meta_path.is_file(), f"Missing metadata file for {sid}: {meta_path.name}")
    require(samples_path.is_file(), f"Missing CPU samples for {sid}: {samples_path.name}")
    fields, rows = read_csv(path)
    require(NEEDED <= fields, f"{sid}: missing fields {sorted(NEEDED-fields)}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    sample_fields, samples = read_csv(samples_path)
    require("cpu_utilization_pct" in sample_fields, f"{sid}: CPU sample column missing")
    require(meta.get("error") is None, f"{sid}: metadata error={meta.get('error')!r}")
    require(meta.get("raw_csv") == path.name, f"{sid}: metadata raw_csv mismatch")
    require(meta.get("cpu_samples_csv") == samples_path.name,
            f"{sid}: metadata CPU samples filename mismatch")
    require(meta.get("scenario_id") == sid, f"{sid}: metadata scenario mismatch")
    require(meta.get("run_type") == "planned", f"{sid}: only full planned runs qualify")
    require(meta.get("dataset_split") == planned["dataset_split"],
            f"{sid}: metadata train/test split mismatch")
    require(meta.get("measurement_method") == METHOD, f"{sid}: wrong timing method")
    require(bool(re.fullmatch(r"[a-fA-F0-9]{64}", str(meta.get("probe_executable_sha256", "")))),
            f"{sid}: probe SHA-256 missing/invalid")
    require(isinstance(meta.get("plan"), dict), f"{sid}: metadata plan is absent")
    for key, value in planned.items():
        require(str(meta["plan"].get(key)) == str(value),
                f"{sid}: metadata plan changed at {key}")

    reps = int(planned["repetitions_per_profile"])
    expected_n = reps * len(GROUPS)
    require(len(rows) == expected_n, f"{sid}: {len(rows)} rows, expected {expected_n}")
    require(meta.get("intended_repetitions_per_profile") == reps,
            f"{sid}: unexpected repetitions in metadata")
    require(meta.get("measured_trials") == len(rows), f"{sid}: metadata trial count mismatch")
    flags = [r["success"].strip().lower() for r in rows]
    require(all(flag in {"true", "false"} for flag in flags),
            f"{sid}: invalid boolean success flag")
    successes = sum(flag == "true" for flag in flags)
    failures = len(rows) - successes
    require(meta.get("successes") == successes,
            f"{sid}: metadata successes {meta.get('successes')} != CSV successes {successes}")
    expected = {(group, rep) for group in GROUPS for rep in range(1, reps+1)}
    actual = [(r["profile"], int(r["repetition"])) for r in rows]
    require(len(actual) == len(set(actual)) and set(actual) == expected,
            f"{sid}: missing/duplicate profile+repetition tuples")
    orders = [int(r["trial_order_in_scenario"]) for r in rows]
    require(sorted(orders) == list(range(1, expected_n+1)),
            f"{sid}: unexpected trial order field")

    numeric_plan_fields = {
        "cpu_load_target_pct": "cpu_load_target_pct",
        "cpu_stress_requested_pct": "cpu_stress_request_pct",
        "memory_limit_mb": "memory_ceiling_mib",
        "rtt_target_ms": "rtt_target_ms",
        "bandwidth_target_mbps": "bandwidth_target_mbps",
        "packet_loss_target_pct": "packet_loss_target_pct",
    }
    rtt = number(planned["rtt_target_ms"], "RTT target")
    tolerance = max(5.0, 0.05 * rtt)
    net = meta.get("network_validation")
    require(isinstance(net, dict), f"{sid}: network validation missing")
    observed_rtt = number(net.get("rtt_observed_ms"), f"{sid} RTT")
    require(abs(observed_rtt-rtt) <= tolerance + 0.000001,
            f"{sid}: RTT deviation {observed_rtt-rtt:+.3f}ms exceeds ±{tolerance:g}ms")
    sent = int(net.get("validation_ping_packets_sent"))
    received = int(net.get("validation_ping_packets_received"))
    observed_loss = number(net.get("validation_ping_loss_pct"), f"{sid}: ping loss")
    require(0 <= received <= sent and sent >= 10 and 0 <= observed_loss <= 100,
            f"{sid}: invalid network ping measurements")
    require(abs(observed_loss - 100*(sent-received)/sent) <= 0.11,
            f"{sid}: observed ping loss disagrees with packet count")
    require(int(meta.get("memory_max_verified_bytes")) == int(number(planned["memory_ceiling_mib"], "memory")*1048576),
            f"{sid}: incorrect cgroup memory limit")
    same_number(meta.get("cpu_requested_pct"), planned["cpu_stress_request_pct"],
                f"{sid}: requested CPU")

    for n, row in enumerate(rows, start=2):
        label = f"{sid} CSV line {n}"
        require(row["scenario_id"] == sid, f"{label}: scenario mismatch")
        require(row["device_id"] == planned["device_id"], f"{label}: device mismatch")
        require(row["dataset_split"] == planned["dataset_split"], f"{label}: split mismatch")
        require(row["run_type"] == "planned", f"{label}: wrong run_type")
        require(row["measurement_scope"] == SCOPE, f"{label}: scope mismatch")
        require(row["measurement_method"] == METHOD, f"{label}: timing method mismatch")
        good = row["success"].strip().lower() == "true"
        if good:
            require(row["negotiated_group"] == row["profile"], f"{label}: group mismatch")
            require(not row["error_reason"].strip(), f"{label}: successful TLS has error_reason")
        else:
            require(bool(row["error_reason"].strip()),
                    f"{label}: failed TLS attempt has no error_reason")
        for col in ("cpu_control_applied", "memory_control_applied"):
            require(row[col].strip().lower() == "true", f"{label}: {col} not applied")
        for col, field in numeric_plan_fields.items():
            same_number(row[col], planned[field], f"{label} {col}")
        same_number(row["memory_max_verified_bytes"], meta["memory_max_verified_bytes"],
                    f"{label}: cgroup bytes")
        same_number(row["rtt_observed_ms"], observed_rtt, f"{label}: RTT observed")
        same_number(row["validation_ping_packets_sent"], sent, f"{label}: pings sent")
        same_number(row["validation_ping_packets_received"], received, f"{label}: pings received")
        same_number(row["validation_ping_loss_pct"], observed_loss, f"{label}: ping loss")
        require(number(row["elapsed_ms"], f"{label} elapsed_ms") > 0,
                f"{label}: elapsed_ms must be positive even on a failed attempt")
        if good:
            require(number(row["tcp_ms"], f"{label} tcp_ms") > 0 and
                    number(row["tls_handshake_ms"], f"{label} tls_handshake_ms") > 0,
                    f"{label}: missing or invalid successful-stage timing")
            # Process wall-time should not be below TCP+TLS beyond coarse rounding.
            require(number(row["elapsed_ms"], f"{label} elapsed") + 0.02 >=
                    number(row["tcp_ms"], f"{label} tcp") + number(row["tls_handshake_ms"], f"{label} TLS"),
                    f"{label}: process time shorter than TCP+TLS; check timing capture")
        else:
            # v3 runner intentionally blanks phase timings for unsuccessful trials.
            require(not row["tcp_ms"].strip() and not row["tls_handshake_ms"].strip(),
                    f"{label}: failed attempt must not carry unverified phase timings")
        require(row["bandwidth_measurement_status"] == "configured_not_measured_this_run",
                f"{label}: unexpected throughput status")
        loss_direction = number(row["netem_loss_per_direction_pct"], f"{label}: netem loss")
        loss_pct = number(planned["packet_loss_target_pct"], "loss target")
        expected_direction = 100*(1-math.sqrt(1-loss_pct/100))
        require(abs(loss_direction - expected_direction) < 0.01,
                f"{label}: netem per-direction loss mismatch")

    sample_values = [number(s["cpu_utilization_pct"], f"{sid}: CPU sample") for s in samples]
    require(all(0 <= v <= 100 for v in sample_values), f"{sid}: CPU sample out of range")
    require(len(sample_values) >= 15, f"{sid}: only {len(sample_values)} CPU samples; too few")
    require(meta.get("cpu_monitor_sample_count") == len(sample_values),
            f"{sid}: CPU sample count mismatch")
    cpu_avg = number(meta.get("cpu_over_run_pct"), f"{sid}: monitored CPU")
    require(abs(cpu_avg - mean(sample_values)) <= 0.11,
            f"{sid}: CPU average differs from the saved CPU monitoring series")
    cpu_target = number(planned["cpu_load_target_pct"], "CPU target")
    require(abs(cpu_avg - cpu_target) <= 7.5,
            f"{sid}: average CPU {cpu_avg}% too far from intended {cpu_target}% (±7.5pp diagnostic bound)")
    print(f"PASS {sid} [{planned['dataset_split']}]: {len(rows)} attempts, "
          f"{successes} successful, {failures} failed; "
          f"CPU {cpu_avg:.2f}% (target {cpu_target:g}%), RTT {observed_rtt:.3f}ms "
          f"(target {rtt:g}ms), {len(sample_values)} CPU samples")
    for g in GROUPS:
        trials = [row for row in rows if row["profile"] == g]
        good_trials = [row for row in trials if row["success"].strip().lower() == "true"]
        ts = [number(row["tls_handshake_ms"], "tls") for row in good_trials]
        latency = (f"TLS median {median(ts):8.3f} ms | max {max(ts):8.3f} ms"
                   if ts else "TLS latency unavailable (no successful handshakes)")
        print(f"    {g:<16} success {len(good_trials):2d}/{len(trials):2d} | {latency}")
    if failures:
        print("    Note: unsuccessful TLS attempts retained in success denominator; "
              "no imputed handshake latency")
    if number(planned["packet_loss_target_pct"], "loss") > 0:
        print(f"    Configured loss {planned['packet_loss_target_pct']}%, "
              f"observed ping loss {observed_loss:g}% ({sent} packets); "
              "random variation and other causes can affect observed loss")
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", help="Validate exactly one scenario, e.g. C027")
    group.add_argument("--all", action="store_true", help="Validate all collected full batches and list pending scenarios")
    args = parser.parse_args()
    plan = load_plan()
    files = full_csv_files()
    if args.scenario:
        require(args.scenario in plan, f"Scenario {args.scenario} not in plan")
        require(args.scenario in files, f"{args.scenario}: no full-batch CSV found in {RAW}")
        validate_one(args.scenario, files[args.scenario], plan[args.scenario])
        print("Structural and control validation: PASSED")
        return
    unknown = set(files) - set(plan)
    require(not unknown, f"Found unknown scenario(s): {sorted(unknown)}")
    total, passed, failures, pending = 0, [], [], []
    ordered = sorted(plan, key=lambda sid: int(plan[sid]["execution_order"]))
    for sid in ordered:
        if sid not in files:
            pending.append(sid)
            continue
        try:
            total += validate_one(sid, files[sid], plan[sid])
            passed.append(sid)
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            failures.append((sid, str(exc)))
            print(f"FAIL {sid}: {exc}")
    print(f"\nSUMMARY: {len(passed)} scenarios passed, {len(failures)} failed, "
          f"{len(pending)} not collected; {total} validated trials")
    next_dev = next((s for s in pending if plan[s]["dataset_split"] == "development"), None)
    if next_dev:
        print(f"Next uncollected development scenario by execution order: {next_dev}")
    if failures:
        raise ValueError("One or more collected scenarios failed validation; see FAIL lines")
    print("Collected-batch validation: PASSED (missing scenarios remain pending)")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
