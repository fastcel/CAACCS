#!/usr/bin/env python3
"""Reproducibly audit and snapshot Phase 1C development data (NO held-out reads).

Run from the repository root:
  python3 phase1c_validate_v2.py --all
  python3 phase1c_finalize_development_v1.py

Produces immutable-by-default derived artifacts under data/phase1c_dataset_v1/.
Raw measurements are never modified. Standard library only.
"""

import csv
import hashlib
import io
import itertools
import json
import math
import re
from collections import Counter
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "phase1c_raw"
OUT = DATA / "phase1c_dataset_v1"
PLAN = DATA / "phase1c_experiment_plan.csv"
GROUPS = ("X25519", "MLKEM768", "X25519MLKEM768")
FILE_RE = re.compile(r"^phase1c_planned_probe_(C\d{3})_\d{8}_\d{6}_\d{6}\.csv$")
FIELDS = (
    "scenario_id", "dataset_split", "device_id", "cpu_load_target_pct",
    "cpu_over_run_pct", "memory_ceiling_mib", "rtt_target_ms",
    "rtt_observed_ms", "bandwidth_target_mbps", "packet_loss_target_pct",
    "validation_ping_loss_pct", "profile", "attempts", "successes", "failures",
    "success_fraction", "tcp_median_success_ms", "tls_median_success_ms",
    "tls_p95_nearest_rank_success_ms", "tls_max_success_ms",
)
SOURCE_NAMES = (
    "phase1c_combined_runner_v3.py",
    "phase1c_validate_v2.py",
    "phase1c_tls_probe.c",
    "phase1c_finalize_development_v1.py",
    "PHASE1C_DATASET_SPEC_v1.md",
)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def rows_of(path):
    with path.open(newline="", encoding="utf-8-sig") as fp:
        reader = csv.DictReader(fp)
        require(reader.fieldnames is not None, f"No CSV header: {path}")
        require(len(reader.fieldnames) == len(set(reader.fieldnames)),
                f"Duplicate headers: {path}")
        rows = list(reader)
    require(all(None not in r and None not in r.values() for r in rows),
            f"Malformed CSV: {path}")
    return rows


def finite_nonnegative(value, label):
    result = float(value)
    require(math.isfinite(result) and result >= 0, f"Bad {label}: {value!r}")
    return result


def csv_output(columns, rows):
    fp = io.StringIO(newline="")
    writer = csv.DictWriter(fp, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return fp.getvalue()


def save_without_overwriting(path, contents):
    data = contents.encode("utf-8")
    if path.exists():
        require(path.read_bytes() == data,
                f"Frozen output differs: {path} (investigate; do not overwrite v1)")
    else:
        path.write_bytes(data)


def format_num(value):
    return "" if value is None else f"{value:.6f}"


def nearest_rank_p95(values):
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None


def main():
    plan_rows = rows_of(PLAN)
    require(len(plan_rows) == 36, "Expected 36 planned scenarios")
    plan = {r["scenario_id"]: r for r in plan_rows}
    require(len(plan) == 36, "Duplicate scenario IDs")
    dev = {s for s, p in plan.items() if p["dataset_split"] == "development"}
    held = {s for s, p in plan.items() if p["dataset_split"] == "held_out_context"}
    require(len(dev) == 30 and len(held) == 6 and dev.isdisjoint(held),
            "Expected exactly 30 development and 6 held-out scenarios")
    require(set(plan) == dev | held, "Unexpected dataset split label")

    expected_contexts = set(itertools.product((20, 50, 80), (10, 50, 150),
                                               (5, 100), (0.0, 2.0)))
    actual_contexts = {
        (int(p["cpu_load_target_pct"]), int(p["rtt_target_ms"]),
         int(p["bandwidth_target_mbps"]), float(p["packet_loss_target_pct"]))
        for p in plan_rows
    }
    require(actual_contexts == expected_contexts, "Plan does not cover 36 unique contexts")

    raw_files = {}
    for path in RAW.glob("phase1c_planned_probe_*.csv"):
        match = FILE_RE.fullmatch(path.name)
        if match:
            raw_files.setdefault(match.group(1), []).append(path)
    require(set(raw_files) == dev, f"Missing={sorted(dev-set(raw_files))}; "
            f"unexpected raw batches={sorted(set(raw_files)-dev)}")
    require(all(len(paths) == 1 for paths in raw_files.values()),
            "Duplicate full batch exists: investigate before freezing")

    summary = []
    source_paths = [PLAN]
    for source in SOURCE_NAMES:
        p = ROOT / source
        require(p.is_file(), f"Missing source/protocol file: {p}")
        source_paths.append(p)

    total_success = 0
    total_failed = 0
    cpu_means = []
    for sid in sorted(dev):
        p = plan[sid]
        raw = raw_files[sid][0]
        prefix = raw.with_suffix("")
        metadata = Path(str(prefix) + "_metadata.json")
        samples = Path(str(prefix) + "_cpu_samples.csv")
        require(metadata.is_file() and samples.is_file(), f"Missing companion: {sid}")
        source_paths.extend((raw, metadata, samples))
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        require(meta["scenario_id"] == sid and
                meta["dataset_split"] == "development" and
                meta["run_type"] == "planned", f"Bad metadata identity: {sid}")
        require(meta["plan"] == p, f"Metadata plan mismatch: {sid}")
        require(meta["measurement_method"] == "openssl_ssl_connect_probe_v1",
                f"Unexpected timing method: {sid}")
        require(int(meta["measured_trials"]) == 60, f"Metadata count mismatch: {sid}")
        cpu_rows = rows_of(samples)
        require(len(cpu_rows) >= 15, f"Too few CPU samples: {sid}")
        require(int(meta["cpu_monitor_sample_count"]) == len(cpu_rows),
                f"CPU sample count mismatch: {sid}")
        cpu = finite_nonnegative(meta["cpu_over_run_pct"], f"CPU in {sid}")
        require(abs(cpu - float(p["cpu_load_target_pct"])) <= 7.5,
                f"CPU outside full-run tolerance: {sid}")
        cpu_means.append(cpu)
        net = meta["network_validation"]
        rtt = finite_nonnegative(net["rtt_observed_ms"], f"RTT in {sid}")
        target_rtt = float(p["rtt_target_ms"])
        require(abs(rtt - target_rtt) <= max(5.0, target_rtt * .05),
                f"RTT outside tolerance: {sid}")
        require(int(meta["memory_max_verified_bytes"]) == 4096 * 1024**2,
                f"Memory ceiling not verified: {sid}")

        observations = rows_of(raw)
        require(len(observations) == 60, f"Expected 60 attempts: {sid}")
        require({r["dataset_split"] for r in observations} == {"development"},
                f"Wrong split in raw: {sid}")
        require({r["scenario_id"] for r in observations} == {sid},
                f"Wrong scenario ID in raw: {sid}")
        require({r["measurement_method"] for r in observations} ==
                {"openssl_ssl_connect_probe_v1"}, f"Mixed methods: {sid}")
        require({r["run_type"] for r in observations} == {"planned"},
                f"Non-full-run observations: {sid}")
        require({int(r["trial_order_in_scenario"]) for r in observations} ==
                set(range(1, 61)), f"Duplicate or missing trial order: {sid}")
        require(len({r["run_id"] for r in observations}) == 60,
                f"Duplicate run IDs: {sid}")

        success_count = 0
        for group in GROUPS:
            group_rows = [r for r in observations if r["profile"] == group]
            require(len(group_rows) == 20, f"Wrong profile count: {sid}/{group}")
            require({int(r["repetition"]) for r in group_rows} ==
                    set(range(1, 21)), f"Missing/duplicate repetitions: {sid}/{group}")
            require(all(r["success"].lower() in ("true", "false") for r in group_rows),
                    f"Unexpected success flag: {sid}/{group}")
            good = [r for r in group_rows if r["success"].lower() == "true"]
            bad = [r for r in group_rows if r["success"].lower() == "false"]
            require(all(r["error_reason"].strip() for r in bad),
                    f"Failed trials lack reason: {sid}/{group}")
            require(all(r["negotiated_group"] == group for r in good),
                    f"Unexpected negotiated group: {sid}/{group}")
            tcp = [finite_nonnegative(r["tcp_ms"], "tcp_ms") for r in good]
            tls = [finite_nonnegative(r["tls_handshake_ms"], "tls_handshake_ms")
                   for r in good]
            success_count += len(good)
            summary.append({
                "scenario_id": sid,
                "dataset_split": "development",
                "device_id": p["device_id"],
                "cpu_load_target_pct": p["cpu_load_target_pct"],
                "cpu_over_run_pct": format_num(cpu),
                "memory_ceiling_mib": p["memory_ceiling_mib"],
                "rtt_target_ms": p["rtt_target_ms"],
                "rtt_observed_ms": format_num(rtt),
                "bandwidth_target_mbps": p["bandwidth_target_mbps"],
                "packet_loss_target_pct": p["packet_loss_target_pct"],
                "validation_ping_loss_pct": net["validation_ping_loss_pct"],
                "profile": group,
                "attempts": len(group_rows),
                "successes": len(good),
                "failures": len(bad),
                "success_fraction": format_num(len(good) / len(group_rows)),
                "tcp_median_success_ms": format_num(median(tcp) if tcp else None),
                "tls_median_success_ms": format_num(median(tls) if tls else None),
                "tls_p95_nearest_rank_success_ms": format_num(
                    nearest_rank_p95(tls)),
                "tls_max_success_ms": format_num(max(tls) if tls else None),
            })
        require(int(meta["successes"]) == success_count,
                f"Raw/metadata success mismatch: {sid}")
        total_success += success_count
        total_failed += 60-success_count

    require(len(summary) == 90 and total_success + total_failed == 1800,
            "Unexpected derived dataset size")
    require(total_failed == 0, "Development data differ from validated 0-failure snapshot")

    manifest_rows = []
    for path in sorted(source_paths, key=lambda path: str(path.relative_to(ROOT))):
        raw_bytes = path.read_bytes()
        manifest_rows.append({
            "relative_path": path.relative_to(ROOT).as_posix(),
            "bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        })

    output_summary = csv_output(FIELDS, summary)
    output_manifest = csv_output(("relative_path", "bytes", "sha256"), manifest_rows)
    report = "\n".join((
        "PHASE 1C DEVELOPMENT DATASET V1 — FINALIZATION REPORT",
        "Status: PASS (development dataset only)",
        "Scenarios: 30 of 30 development",
        "Attempted connections: 1800",
        f"Successful connections: {total_success}",
        f"Failed connections: {total_failed}",
        "Derived scenario-by-profile rows: 90",
        "CPU samples per scenario: >= 15; observed CPU/RTT within validation tolerances",
        "Three source files per batch: raw CSV, CPU samples, and JSON metadata",
        "Plan: 36 factorial contexts; exactly 6 held-out scenarios with no raw batches",
        "Held-out IDs: " + ", ".join(sorted(held)),
        "Source file hashes: SHA-256; manifest lists original bytes and filenames",
        "Missing measurements are not imputed; no observations removed or winsorized",
        "Only successful TLS attempts contribute to latency summaries; all attempts to success rate",
        "p95: nearest-rank (ceil(0.95 * n)) of successful TLS handshakes",
        "Bandwidth: configured, not measured; memory: ceiling, not RAM-pressure experiment",
        "CPU: shared-host contention; TLS includes network RTT and scheduling",
        "No labels, oracle winners, ML predictions, or held-out outcomes generated",
        "Next: review and commit v1 source files and outputs; finalize evaluation protocol",
        "", "FINALIZATION AUDIT COMPLETE", "",
    ))

    OUT.mkdir(parents=True, exist_ok=True)
    save_without_overwriting(OUT / "development_profile_summary.csv", output_summary)
    save_without_overwriting(OUT / "development_source_manifest.csv", output_manifest)
    save_without_overwriting(OUT / "development_audit_report.txt", report)
    print(report)
    print("Artifacts:")
    for filename in ("development_profile_summary.csv", "development_source_manifest.csv",
                     "development_audit_report.txt"):
        print(f"  {OUT / filename}")


if __name__ == "__main__":
    main()
