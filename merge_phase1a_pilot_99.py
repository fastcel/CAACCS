#!/usr/bin/env python3
"""Consolidate FYP Phase 1A pilot observations, without changing source CSVs.

Run from the FYP project after placing this script next to data/scenarios.csv:
    python3 merge_phase1a_pilot_99.py

Explicit source manifest avoids silently counting both S005 pilot runs.  Missing
measurements are left empty, not filled with fabricated values.  The output is
an audited *mixed-control pilot*, NOT an ML training dataset.
"""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import csv
import statistics
import sys

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
NETWORK_FILE = "raw_network_conditions_pilot_combined_63.csv"
CPU_FILES = {
    "S002": "raw_cpu_memory_pilot_S002_20261019_165955_050059.csv",
    "S003": "raw_cpu_memory_pilot_S003_20261019_170525_591988.csv",
    "S004": "raw_cpu_memory_pilot_S004_20261019_174138_164651.csv",
    # The S005 filename confirmed by the successful user terminal output.
    "S005": "raw_cpu_memory_pilot_S005_20261019_174707_306217.csv",
}
SCENARIOS = tuple(f"S{i:03d}" for i in range(1, 12))
PROFILES = ("X25519", "MLKEM768", "X25519MLKEM768")
COMMON_FIRST = [
    "run_id", "original_run_id", "source_csv", "merge_input_csv",
    "scenario_id", "device_id", "profile", "repetition",
    "trial_order_in_scenario", "timestamp_utc",
    "cpu_load_target_pct", "cpu_control_applied",
    "cpu_stress_requested_pct", "cpu_method", "cpu_workers",
    "cpu_observed_during_trial_pct_approx",
    "cpu_observed_over_benchmark_pct", "cpu_monitor_samples",
    "memory_limit_mb", "memory_control_applied",
    "memory_max_verified_bytes", "cgroup_memory_peak_mb",
    "rtt_target_ms", "rtt_observed_ms", "validation_ping_loss_pct",
    "bandwidth_target_mbps", "bandwidth_measurement_status",
    "packet_loss_target_pct", "netem_loss_per_direction_pct",
    "elapsed_ms", "cpu_time_ms", "peak_memory_mb",
    "bytes_read", "bytes_written", "negotiated_group", "success",
    "error_reason", "measurement_scope",
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_csv(path):
    require(path.is_file(), f"Missing input CSV: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        require(bool(fields), f"Missing header: {path.name}")
        require(len(fields) == len(set(fields)), f"Duplicate header fields: {path.name}")
        rows = list(reader)
    for idx, row in enumerate(rows, 2):
        require(None not in row, f"Extra CSV values in {path.name}, line {idx}")
        require(all(value is not None for value in row.values()),
                f"Missing CSV values in {path.name}, line {idx}")
    return fields, rows


def scenario_reference():
    fields, rows = read_csv(DATA / "scenarios.csv")
    required = {
        "scenario_id", "device_id", "cpu_load_target_pct", "memory_limit_mb",
        "rtt_target_ms", "bandwidth_target_mbps", "packet_loss_target_pct",
    }
    require(required.issubset(fields), "scenarios.csv is missing required columns")
    result = {}
    for row in rows:
        sid = row["scenario_id"]
        require(sid not in result, f"Duplicate scenario in scenarios.csv: {sid}")
        result[sid] = row
    require(set(result) == set(SCENARIOS), "scenarios.csv must define S001 through S011")
    return result


def same_number(value, expected):
    try:
        return abs(float(value) - float(expected)) < 1e-8
    except (TypeError, ValueError):
        return False


def main():
    ref = scenario_reference()
    inputs = [(NETWORK_FILE, 63, set(SCENARIOS) - set(CPU_FILES), "network")]
    inputs.extend((filename, 9, {sid}, "cpu_memory") for sid, filename in CPU_FILES.items())
    all_rows = []
    fields_seen = set()
    fields_order = []
    source_messages = []
    for name, expected_rows, allowed_scenarios, kind in inputs:
        fields, rows = read_csv(DATA / name)
        require(len(rows) == expected_rows,
                f"{name}: expected {expected_rows} rows, found {len(rows)}")
        must_have = {
            "run_id", "scenario_id", "device_id", "profile", "repetition",
            "elapsed_ms", "cpu_time_ms", "peak_memory_mb", "success",
            "memory_limit_mb", "rtt_target_ms", "bandwidth_target_mbps",
            "packet_loss_target_pct", "measurement_scope",
        }
        require(must_have.issubset(fields), f"Missing critical columns in {name}: {must_have - set(fields)}")
        if kind == "cpu_memory":
            require({"memory_max_verified_bytes", "cpu_observed_over_benchmark_pct",
                     "cpu_monitor_samples", "memory_control_applied"}.issubset(fields),
                    f"CPU/memory fields missing from {name}")
        else:
            require({"cpu_control_applied", "memory_control_applied"}.issubset(fields),
                    "Network pilot lacks explicit control flags")
        for field in fields:
            if field not in fields_seen:
                fields_order.append(field)
                fields_seen.add(field)
        for row in rows:
            sid = row["scenario_id"]
            require(sid in allowed_scenarios, f"{name} has unexpected scenario {sid}")
            require(row["device_id"] == ref[sid]["device_id"],
                    f"Device ID mismatch: {name} / {sid}")
            require(row["profile"] in PROFILES, f"Unexpected TLS profile in {name}")
            require(row["repetition"] in {"1", "2", "3"},
                    f"Unexpected repetition in {name}")
            require(row["success"].strip().lower() == "true",
                    f"Unexpected failed/invalid TLS run in {name}: {row['run_id']}")
            require(row.get("negotiated_group", "") == row["profile"],
                    f"Negotiated group differs from requested profile: {name} / {row['run_id']}")
            for key in ("cpu_load_target_pct", "memory_limit_mb", "rtt_target_ms",
                        "bandwidth_target_mbps", "packet_loss_target_pct"):
                require(same_number(row.get(key), ref[sid][key]),
                        f"Scenario metadata mismatch: {name} / {sid} / {key}")
            for key in ("elapsed_ms", "cpu_time_ms", "peak_memory_mb"):
                require(row[key] and float(row[key]) >= 0,
                        f"Missing or invalid {key}: {name} / {row['run_id']}")
            normalized = dict(row)
            normalized["original_run_id"] = row.get("original_run_id") or row["run_id"]
            normalized["source_csv"] = row.get("source_csv") or name
            normalized["merge_input_csv"] = name
            if kind == "cpu_memory":
                # Derived from this *known* CPU-contention benchmark provenance and
                # verified nonempty stress monitoring; not a direct input CSV field.
                require(row["memory_control_applied"].strip().lower() == "true",
                        f"Memory control not applied: {name}")
                require(int(float(row["memory_max_verified_bytes"])) ==
                        round(float(ref[sid]["memory_limit_mb"]) * 1024**2),
                        f"Wrong cgroup memory.max: {name}")
                require(row["cpu_observed_over_benchmark_pct"].strip() != "" and
                        int(row["cpu_monitor_samples"]) > 0,
                        f"CPU monitoring not present: {name}")
                normalized["cpu_control_applied"] = "True"
                require("cpu_contention" in row["measurement_scope"],
                        f"Incorrect CPU/memory scope label: {name}")
            else:
                require(row["cpu_control_applied"].strip().lower() == "false" and
                        row["memory_control_applied"].strip().lower() == "false",
                        f"Network-only run falsely marked CPU/memory-controlled: {name}")
                require("network_only" in row["measurement_scope"],
                        f"Incorrect network pilot scope label: {name}")
            all_rows.append(normalized)
        source_messages.append(f"  {name}: {len(rows)} records")

    require(len(all_rows) == 99, f"Expected 99 total rows; got {len(all_rows)}")
    observed = Counter((r["scenario_id"], r["profile"], int(r["repetition"]))
                       for r in all_rows)
    expected = Counter({(sid, profile, repetition): 1 for sid in SCENARIOS
                        for profile in PROFILES for repetition in (1, 2, 3)})
    require(observed == expected,
            f"Missing/duplicate scenario-profile-repetition combinations:\n"
            f"  Missing: {list((expected - observed).elements())}\n"
            f"  Extra: {list((observed - expected).elements())}")
    for i, row in enumerate(all_rows, 1):
        row["run_id"] = f"R{i:04d}"

    extra_fields = [field for field in fields_order if field not in COMMON_FIRST]
    output_fields = COMMON_FIRST + extra_fields
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    merged = DATA / f"fyp_phase1a_pilot_99_{stamp}.csv"
    summary = DATA / f"fyp_phase1a_summary_33_{stamp}.csv"
    report = DATA / f"fyp_phase1a_validation_{stamp}.txt"

    stats_rows = []
    for sid in SCENARIOS:
        for profile in PROFILES:
            subset = [r for r in all_rows if r["scenario_id"] == sid and r["profile"] == profile]
            elapsed = [float(r["elapsed_ms"]) for r in subset]
            stats_rows.append({
                "scenario_id": sid, "profile": profile, "n": len(subset),
                "mean_elapsed_ms": f"{statistics.mean(elapsed):.3f}",
                "median_elapsed_ms": f"{statistics.median(elapsed):.3f}",
                "min_elapsed_ms": f"{min(elapsed):.3f}",
                "max_elapsed_ms": f"{max(elapsed):.3f}",
                "success_count": len(subset),
                "measurement_scope": subset[0]["measurement_scope"],
            })

    summary_fields = list(stats_rows[0])
    report_lines = [
        "FYP Phase 1A — Pilot dataset consolidation and validation",
        f"UTC build timestamp: {stamp}",
        "",
        "INPUT FILES (only these five; other S005 pilot file intentionally excluded):",
        *source_messages,
        "",
        "VALIDATION RESULTS:",
        "  Total records: 99",
        "  Scenarios: 11, each has 9 measured runs",
        "  TLS profiles: 3 per scenario, repetitions 1/2/3 present exactly once",
        "  Success flags: 99 true; negotiated groups match requested profiles",
        "  Unique new run IDs: 99",
        "  Scenario targets consistent with data/scenarios.csv",
        "  Memory controller byte ceiling and CPU monitor verified for S002-S005",
        "  Network-only controls explicitly remain false for S001, S006-S011",
        "",
        "RESEARCH LIMITATIONS:",
        "  This is a mixed-control PILOT, not a uniform fully-controlled dataset.",
        "  S001 and S006-S011 did NOT enforce scenario CPU-load or memory limits.",
        "  S002-S005 used shared-host CPU contention and cgroup memory CEILINGS,",
        "  not physical memory pressure; the stress also affects the TLS server.",
        "  Process elapsed_ms includes OpenSSL process startup and TCP/TLS operations;",
        "  it is NOT isolated TLS handshake or key exchange latency.",
        "  Three repetitions per cell are inadequate for ranking algorithms reliably.",
        "  CPU control applied=True for S002-S005 is provenance-derived from the",
        "  CPU workload scripts; it was absent from those source CSV headers.",
        "  Missing/inapplicable field values intentionally remain empty.",
        "  Do not use these pilot measurements as ML training labels.",
        "",
        "OUTPUTS:",
        f"  {merged.name}",
        f"  {summary.name}",
        f"  {report.name}",
        "",
        "SCENARIO COUNTS:",
    ]
    report_lines.extend(f"  {sid}: 9" for sid in SCENARIOS)

    # Exclusive create: fail rather than overwrite existing output files.
    with merged.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(all_rows)
    with summary.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(stats_rows)
    with report.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(report_lines) + "\n")

    print("Phase 1A pilot CSV merged and validated.")
    print("Measured records: 99; scenarios: 11; profiles: 3; successes: 99")
    print("Selected S005:", CPU_FILES["S005"])
    other_s005 = sorted(DATA.glob("raw_cpu_memory_pilot_S005_*.csv"))
    for path in other_s005:
        if path.name != CPU_FILES["S005"]:
            print("Excluded alternate S005 run (preserved):", path.name)
    print("Merged dataset:", merged)
    print("33-group summary:", summary)
    print("Validation report:", report)
    print("Original files were NOT modified.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, csv.Error) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
