#!/usr/bin/env python3
"""Merge the three verified Phase 1B network runs without changing raw files.

Run from the FYP repository root:
    python3 phase1b_merge_network.py
    python3 phase1b_audit.py --input data/phase1b_network_210.csv \
        --out data/phase1b_network_summary_21.csv

This is a *network-only*, process-elapsed-time dataset. It is not a full
CPU/memory-controlled dataset or a source of validated ML training labels.
"""

from __future__ import annotations

from collections import Counter
import csv
import math
from pathlib import Path
import sys

DATA = Path(__file__).resolve().parent / "data"
INPUTS = {
    "raw_network_conditions_pilot_20261107_184833_235908.csv": {"S001"},
    "raw_network_conditions_pilot_20261107_185033_627114.csv": {"S006", "S007", "S008", "S009"},
    "raw_network_conditions_pilot_20261107_190221_097012.csv": {"S010", "S011"},
}
OUTPUT = DATA / "phase1b_network_210.csv"
PROFILES = {"X25519", "MLKEM768", "X25519MLKEM768"}
REPETITIONS = set(range(1, 11))
REQUIRED = {
    "run_id", "scenario_id", "profile", "repetition", "elapsed_ms",
    "success", "negotiated_group", "measurement_scope",
    "cpu_control_applied", "memory_control_applied",
}


def load(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing input file: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if len(set(fields)) != len(fields):
            raise ValueError(f"Duplicate headers in {path.name}")
        if not REQUIRED.issubset(fields):
            raise ValueError(f"{path.name}: missing columns {sorted(REQUIRED - set(fields))}")
        rows = list(reader)
    if not rows or any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError(f"Missing/malformed data in {path.name}")
    return fields, rows


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(
            f"Output already exists: {OUTPUT}. Nothing was overwritten."
        )
    all_rows: list[dict[str, str]] = []
    fields: list[str] | None = None
    counts: Counter = Counter()
    sources: Counter = Counter()
    failures = 0
    for filename, expected_scenarios in INPUTS.items():
        file_fields, rows = load(DATA / filename)
        if fields is None:
            fields = file_fields
        if fields != file_fields:
            raise ValueError(f"{filename}: CSV headers differ from the other batches")
        for line_number, row in enumerate(rows, 2):
            sid, profile = row["scenario_id"], row["profile"]
            prefix = f"{filename}:{line_number}"
            if sid not in expected_scenarios:
                raise ValueError(f"{prefix}: unexpected scenario {sid}")
            if profile not in PROFILES:
                raise ValueError(f"{prefix}: unexpected cryptographic profile {profile}")
            try:
                rep = int(row["repetition"])
                elapsed = float(row["elapsed_ms"])
            except (ValueError, TypeError) as e:
                raise ValueError(f"{prefix}: invalid repetition or elapsed time") from e
            if rep not in REPETITIONS:
                raise ValueError(f"{prefix}: unexpected repetition {rep}")
            if not math.isfinite(elapsed) or elapsed <= 0:
                raise ValueError(f"{prefix}: nonpositive or nonfinite elapsed time")
            flag = row["success"].strip().lower()
            if flag not in ("true", "false"):
                raise ValueError(f"{prefix}: invalid success flag {flag!r}")
            if flag == "true" and row["negotiated_group"] != profile:
                raise ValueError(f"{prefix}: negotiated TLS group does not match {profile}")
            if flag == "false":
                failures += 1  # Preserve failed runs rather than excluding them.
            if row["measurement_scope"] != "network_only_pilot":
                raise ValueError(f"{prefix}: not a network-only measurement")
            for control in ("cpu_control_applied", "memory_control_applied"):
                if row[control].strip().lower() not in ("false", "0"):
                    raise ValueError(f"{prefix}: {control} must be explicitly false")
            counts[(sid, profile, rep)] += 1
            sources[filename] += 1
            # Each raw CSV reuses R00001, etc. Preserve raw IDs and create
            # a globally unique ID for the merged dataset.
            record = dict(row)
            record["source_csv"] = filename
            record["source_run_id"] = row["run_id"]
            record["run_id"] = f"B1R{len(all_rows) + 1:04d}"
            all_rows.append(record)

    expected = {(sid, p, rep) for group in INPUTS.values()
                for sid in group for p in PROFILES for rep in REPETITIONS}
    missing = sorted(expected - set(counts))
    duplicates = [(k, n) for k, n in counts.items() if n != 1]
    if missing or duplicates or len(all_rows) != 210:
        raise ValueError(
            f"Dataset incomplete: found {len(all_rows)} rows; "
            f"missing combinations={missing[:5]}, duplicates={duplicates[:5]}"
        )
    assert fields is not None
    new_fields = list(fields) + ["source_csv", "source_run_id"]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # 'x' prevents accidental overwrites, including concurrent reruns.
    with OUTPUT.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=new_fields)
        writer.writeheader()
        writer.writerows(all_rows)
    print("Phase 1B network batches merged and checked.")
    print(f"Rows: {len(all_rows)} | Scenarios: 7 | Profiles: 3 | Repetitions/profile: 10")
    print(f"Successes: {len(all_rows) - failures} | Failures retained: {failures}")
    for filename, count in sources.items():
        print(f"  {filename}: {count} records")
    print(f"Saved: {OUTPUT}")
    print("Original raw CSVs were not changed.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
