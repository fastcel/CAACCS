#!/usr/bin/env python3
"""Merge the Phase 1B 210 network trials and selected 120 resource trials.

Run from your CAACCS/FYP project root:
    python3 phase1b_merge_all.py
    python3 phase1b_audit.py --input data/phase1b_full_330.csv \
        --out data/phase1b_full_summary_33.csv

The two Phase 1B scopes remain explicitly identifiable in the merged CSV:
  - network_only_pilot: network shaping; NO CPU or memory controls
  - shared_host_cpu_contention_memory_ceiling_pilot: CPU contention and
    verified memory ceiling (NOT simulated RAM pressure)

`elapsed_ms` measures OpenSSL process startup plus TCP/TLS work, not just
cryptographic key exchange or the TLS handshake. These are experimental
measurements, not validated machine-learning training labels.

Only the seven-network-scenario combined file and the FOUR selected resource
runs are included. In particular, the earlier S003 ~90% CPU run is excluded.
Input CSVs are never changed. Output is never overwritten.
"""

from collections import Counter
import csv
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = DATA / "phase1b_full_330.csv"
NETWORK_INPUT = "phase1b_network_210.csv"
RESOURCE_INPUTS = {
    "S002": "raw_cpu_memory_pilot_S002_20261108_084542_223648.csv",
    # This is the CORRECTED ~80% observed CPU run; exclude the older ~90% run.
    "S003": "raw_cpu_memory_pilot_S003_20261108_095815_165345.csv",
    "S004": "raw_cpu_memory_pilot_S004_20261108_104712_708101.csv",
    "S005": "raw_cpu_memory_pilot_S005_20261108_105037_272980.csv",
}
NETWORK_SCENARIOS = {"S001", "S006", "S007", "S008", "S009", "S010", "S011"}
PROFILES = {"X25519", "MLKEM768", "X25519MLKEM768"}
REPS = set(range(1, 11))
REQUIRED = {
    "run_id", "scenario_id", "device_id", "profile", "repetition",
    "success", "elapsed_ms", "negotiated_group", "measurement_scope",
    "cpu_load_target_pct", "memory_limit_mb", "rtt_target_ms",
    "bandwidth_target_mbps", "packet_loss_target_pct",
}
EXPECTED_CPU = {"S002": 50, "S003": 80, "S004": 20, "S005": 20}
EXPECTED_MEM = {"S002": 4096, "S003": 4096, "S004": 2048, "S005": 1024}


def load_csv(filename, extra_required=(), required=None):
    path = DATA / filename
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path}; check that the file is in FYP/data/")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError(f"{filename}: duplicate CSV header")
        missing = ((REQUIRED if required is None else set(required)) | set(extra_required)) - set(fields)
        if missing:
            raise ValueError(f"{filename}: missing columns {sorted(missing)}")
        rows = list(reader)
    if not rows or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError(f"{filename}: blank or malformed CSV records")
    return fields, rows


def check_float(record, field, expected, label):
    try:
        value = float(record[field])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"{label}: invalid {field}={record.get(field)!r}") from exc
    if not math.isfinite(value) or abs(value - expected) > 0.00001:
        raise ValueError(f"{label}: {field}={value} differs from scenario definition {expected}")


def main():
    if OUT.exists():
        raise FileExistsError(f"Output already exists: {OUT} (not overwriting)")

    scenario_fields, scenario_rows = load_csv("scenarios.csv", required={"scenario_id", "device_id", "cpu_load_target_pct", "memory_limit_mb", "rtt_target_ms", "bandwidth_target_mbps", "packet_loss_target_pct"})
    scenario_defs = {row["scenario_id"]: row for row in scenario_rows}
    if len(scenario_defs) != len(scenario_rows):
        raise ValueError("scenarios.csv contains duplicate scenario IDs")

    inputs = [(NETWORK_INPUT, NETWORK_SCENARIOS, False)]
    inputs.extend((name, {sid}, True) for sid, name in RESOURCE_INPUTS.items())
    all_records = []
    field_order = []
    counters = Counter()
    input_counts = Counter()
    failures = 0
    known_scenarios = NETWORK_SCENARIOS | set(RESOURCE_INPUTS)

    for filename, allowed, resource_mode in inputs:
        extra = {"memory_control_applied", "memory_max_verified_bytes",
                 "cpu_observed_over_benchmark_pct", "cpu_monitor_samples",
                 "cpu_stress_requested_pct"} if resource_mode else {
                     "cpu_control_applied", "memory_control_applied",
                     "source_csv", "source_run_id"
                 }
        fields, rows = load_csv(filename, extra)
        for field in fields:
            if field not in field_order:
                field_order.append(field)
        for line_no, raw in enumerate(rows, 2):
            sid, profile = raw["scenario_id"], raw["profile"]
            label = f"{filename}:{line_no}"
            if sid not in allowed or profile not in PROFILES:
                raise ValueError(f"{label}: unexpected scenario/profile {sid}/{profile}")
            if sid not in scenario_defs:
                raise ValueError(f"{label}: scenario missing from scenarios.csv")
            try:
                rep = int(raw["repetition"])
                elapsed = float(raw["elapsed_ms"])
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{label}: invalid repetition/elapsed_ms") from exc
            if rep not in REPS or not math.isfinite(elapsed) or elapsed <= 0:
                raise ValueError(f"{label}: invalid repetition/elapsed_ms")
            flag = raw["success"].strip().lower()
            if flag not in ("true", "false"):
                raise ValueError(f"{label}: invalid success flag {flag!r}")
            if flag == "true" and raw["negotiated_group"] != profile:
                raise ValueError(f"{label}: TLS negotiated group differs from {profile}")
            failures += flag == "false"

            reference = scenario_defs[sid]
            if raw["device_id"] != reference["device_id"]:
                raise ValueError(f"{label}: device_id differs from scenarios.csv")
            for key in ("cpu_load_target_pct", "memory_limit_mb", "rtt_target_ms",
                        "bandwidth_target_mbps", "packet_loss_target_pct"):
                check_float(raw, key, float(reference[key]), label)

            rec = dict(raw)
            if resource_mode:
                if raw["measurement_scope"] != "shared_host_cpu_contention_memory_ceiling_pilot":
                    raise ValueError(f"{label}: unexpected resource measurement scope")
                if raw["memory_control_applied"].strip().lower() != "true":
                    raise ValueError(f"{label}: memory ceiling was not marked applied")
                check_float(raw, "memory_max_verified_bytes", EXPECTED_MEM[sid] * 1024**2, label)
                check_float(raw, "cpu_stress_requested_pct", EXPECTED_CPU[sid], label)
                try:
                    observed_cpu = float(raw["cpu_observed_over_benchmark_pct"])
                    samples = int(raw["cpu_monitor_samples"])
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"{label}: missing CPU monitoring") from exc
                if not math.isfinite(observed_cpu) or samples <= 0:
                    raise ValueError(f"{label}: invalid CPU monitoring")
                if abs(observed_cpu - EXPECTED_CPU[sid]) > 7:
                    raise ValueError(f"{label}: observed CPU {observed_cpu}% is not close to S00x target; "
                                     "check that you selected the corrected dataset")
                # CPU control is provenance-derived: shared-host CPU contention,
                # NOT an exact quota on individual TLS processes.
                rec["cpu_control_applied"] = "True"
                rec["cpu_control_provenance"] = "shared_host_stress_ng_contention"
                rec["source_csv"] = filename
                rec["source_run_id"] = raw["run_id"]
            else:
                if raw["measurement_scope"] != "network_only_pilot":
                    raise ValueError(f"{label}: unexpected network measurement scope")
                for control in ("cpu_control_applied", "memory_control_applied"):
                    if raw[control].strip().lower() not in ("false", "0"):
                        raise ValueError(f"{label}: {control} must be false in network-only rows")
                rec["cpu_control_provenance"] = "not_applied"
                # Keep source_csv and source_run_id from the *raw* network batch,
                # not just the intermediary 210-row merged file.
            rec["input_csv"] = filename
            rec["run_id"] = f"B1R{len(all_records) + 1:04d}"
            all_records.append(rec)
            counters[(sid, profile, rep)] += 1
            input_counts[filename] += 1

    expected = {(sid, profile, rep) for sid in known_scenarios
                for profile in PROFILES for rep in REPS}
    missing = expected - set(counters)
    duplicates = [(key, count) for key, count in counters.items() if count != 1]
    if len(all_records) != 330 or missing or duplicates or len(counters) != 330:
        raise ValueError(f"Incomplete/duplicate dataset: rows={len(all_records)} "
                         f"missing={sorted(missing)[:6]}, duplicates={duplicates[:6]}")
    if input_counts[NETWORK_INPUT] != 210 or any(input_counts[x] != 30 for x in RESOURCE_INPUTS.values()):
        raise ValueError(f"Wrong per-input row counts: {dict(input_counts)}")

    preferred = ["run_id", "source_run_id", "source_csv", "input_csv", "scenario_id",
                 "device_id", "profile", "repetition", "timestamp_utc", "elapsed_ms",
                 "success", "negotiated_group", "measurement_scope", "cpu_control_applied",
                 "cpu_control_provenance", "memory_control_applied"]
    fields = preferred + [x for x in field_order if x not in preferred]
    with OUT.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(all_records)
    print("Phase 1B full dataset merged and validated.")
    print(f"Rows: {len(all_records)} | Scenarios: {len(known_scenarios)} | "
          f"Profiles: {len(PROFILES)} | Repetitions/cell: {len(REPS)}")
    print(f"Successes: {len(all_records) - failures} | Failures retained: {failures}")
    for filename, count in input_counts.items():
        print(f"  {filename}: {count} records")
    print(f"Saved: {OUT}")
    print("All raw inputs were preserved. The older miscalibrated S003 run was excluded.")
    print("CAUTION: memory ceilings did not create RAM pressure; timings include process startup.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, csv.Error) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
