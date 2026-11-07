#!/usr/bin/env python3
"""CAACCS: audit Phase 1A / Phase 1B TLS benchmark CSV files.

Read-only by default. Uses Python's standard library only.

Examples (run from your FYP repository root):
    python3 phase1b_audit.py
    python3 phase1b_audit.py --input data/raw_network_conditions_pilot_YYYY.csv
    python3 phase1b_audit.py --input data/fyp_phase1a_pilot_99_20261019_175608_612890.csv --out data/audit_phase1a.csv

Do NOT treat the lowest three-trial median as a reliable optimum or ML label.
The benchmark's elapsed_ms includes OpenSSL process startup and TCP/TLS work.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from pathlib import Path
import statistics
import sys

DEFAULT_CSV = Path("data/fyp_phase1a_pilot_99_20261019_175608_612890.csv")
REQUIRED = {
    "run_id", "scenario_id", "profile", "repetition", "elapsed_ms",
    "negotiated_group", "success", "measurement_scope",
}
KNOWN_PROFILES = {"X25519", "MLKEM768", "X25519MLKEM768"}
NETWORK_ONLY = {"S001", "S006", "S007", "S008", "S009", "S010", "S011"}
RESOURCE = {"S002", "S003", "S004", "S005"}


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise FileNotFoundError(f"CSV not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows = list(reader)
    if not rows:
        raise ValueError(f"No experimental rows in {path}")
    if not REQUIRED.issubset(fields):
        raise ValueError(f"Missing mandatory columns: {sorted(REQUIRED - set(fields))}")
    if len(fields) != len(set(fields)):
        raise ValueError("Duplicate CSV header names")
    if any(None in row for row in rows):
        raise ValueError("Malformed CSV: one or more rows have more fields than headers")
    return fields, rows


def audit(fields: list[str], rows: list[dict[str, str]]) -> tuple[list[dict], list[str]]:
    issues = []
    group_times = defaultdict(list)
    group_failed = Counter()
    identity = Counter()
    scopes = Counter()
    scenarios = Counter()
    profiles = Counter()
    failed = 0
    missing_cpu_control = 0
    missing_memory_control = 0
    for row_index, row in enumerate(rows, 2):
        sid = (row.get("scenario_id") or "").strip()
        profile = (row.get("profile") or "").strip()
        repetition = (row.get("repetition") or "").strip()
        scope = (row.get("measurement_scope") or "").strip()
        success = (row.get("success") or "").strip().lower() == "true"
        scopes[scope] += 1
        scenarios[sid] += 1
        profiles[profile] += 1
        identity[(sid, profile, repetition)] += 1
        if not sid or not repetition:
            issues.append(f"Line {row_index}: blank scenario ID or repetition")
        if profile not in KNOWN_PROFILES:
            issues.append(f"Line {row_index}: unexpected profile {profile!r}")
        try:
            elapsed = float(row["elapsed_ms"])
            if elapsed <= 0 or elapsed != elapsed or elapsed == float("inf"):
                raise ValueError("nonpositive/nonfinite")
        except (ValueError, TypeError):
            issues.append(f"Line {row_index}: invalid elapsed_ms={row.get('elapsed_ms')!r}")
            elapsed = None
        if not success:
            failed += 1
            group_failed[(sid, profile)] += 1
        else:
            if (row.get("negotiated_group") or "").strip() != profile:
                issues.append(f"Line {row_index}: negotiated group mismatch for {sid}/{profile}")
            if elapsed is not None:
                group_times[(sid, profile)].append(elapsed)
        # Missing metrics aren't filled in. Different experimental scopes matter.
        if "cpu_control_applied" in fields and not (row.get("cpu_control_applied") or "").strip():
            missing_cpu_control += 1
        if "memory_control_applied" in fields and not (row.get("memory_control_applied") or "").strip():
            missing_memory_control += 1
        if sid in NETWORK_ONLY and scope == "network_only_pilot":
            for col in ("cpu_control_applied", "memory_control_applied"):
                if col in fields and (row.get(col) or "").strip().lower() not in ("false", "0"):
                    issues.append(f"Line {row_index}: network-only {col} was not explicitly false")
    dup = [(key, count) for key, count in identity.items() if count > 1]
    if dup:
        issues.append(f"Duplicated scenario/profile/repetition tuples: {dup[:5]}")
    summaries = []
    for sid, profile in sorted(set(group_times) | set(group_failed)):
        values = sorted(group_times[(sid, profile)])
        n = len(values)
        med = statistics.median(values) if n else None
        mean = statistics.mean(values) if n else None
        stdev = statistics.stdev(values) if n >= 2 else None
        cv = (100 * stdev / mean) if stdev is not None and mean else None
        # Nearest-rank empirical 95th percentile; don't overinterpret when n is small.
        p95 = values[max(0, (95 * n + 99) // 100 - 1)] if n else None
        summaries.append({
            "scenario_id": sid, "profile": profile,
            "successes": n, "failures": group_failed[(sid, profile)],
            "mean_elapsed_ms": round(mean, 3) if mean is not None else "",
            "median_elapsed_ms": round(med, 3) if med is not None else "",
            "stdev_elapsed_ms": round(stdev, 3) if stdev is not None else "",
            "cv_pct": round(cv, 1) if cv is not None else "",
            "p95_elapsed_ms": round(p95, 3) if p95 is not None else "",
            "min_elapsed_ms": round(values[0], 3) if n else "",
            "max_elapsed_ms": round(values[-1], 3) if n else "",
            "low_sample_warning": n < 10,
        })
    print(f"Rows: {len(rows)} | Fields: {len(fields)} | Scenarios: {len(scenarios)} | Profiles: {len(profiles)}")
    print(f"Successful: {len(rows) - failed} | Failed/invalid success flag: {failed}")
    print("Scopes:", dict(scopes))
    print("Rows by scenario:", dict(sorted(scenarios.items())))
    if missing_cpu_control or missing_memory_control:
        print(f"Missing control flags: CPU={missing_cpu_control}, memory={missing_memory_control}")
    print(f"Unique scenario/profile/repetition tuples: {len(identity)}")
    print("\nSCENARIO / PROFILE SUMMARY (process elapsed time, not isolated TLS handshake)")
    print(f"{'Scenario':<9} {'Profile':<17} {'N':>3} {'Median ms':>11} {'CV %':>8} {'Max ms':>10}")
    for s in summaries:
        print(f"{s['scenario_id']:<9} {s['profile']:<17} {s['successes']:>3} "
              f"{str(s['median_elapsed_ms']):>11} {str(s['cv_pct']):>8} {str(s['max_elapsed_ms']):>10}")
    print("\nNote: CV is descriptive; 3 runs/cell cannot establish reliable rankings.")
    if issues:
        print("\nCHECKS NEED ATTENTION:")
        for issue in issues[:30]:
            print(" -", issue)
        if len(issues) > 30:
            print(f" ... plus {len(issues) - 30} more")
    else:
        print("\nBasic structural and negotiation checks: PASSED")
    return summaries, issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_CSV,
                        help="Any merged or raw TLS benchmark CSV")
    parser.add_argument("--out", type=Path,
                        help="Optional output CSV containing scenario/profile descriptive stats")
    args = parser.parse_args()
    fields, rows = read_csv(args.input)
    print("Dataset:", args.input)
    summaries, issues = audit(fields, rows)
    if args.out:
        if not summaries:
            print("No summaries to write", file=sys.stderr)
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
            writer.writeheader()
            writer.writerows(summaries)
        print("Summary saved:", args.out)
    return 1 if issues else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, csv.Error) as exc:
        print("ERROR:", exc, file=sys.stderr)
        raise SystemExit(1)
