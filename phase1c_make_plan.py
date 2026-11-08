#!/usr/bin/env python3
"""Generate a prospective Phase 1C CPU x network experiment plan.

Does not run TLS, touch namespaces/qdiscs, or overwrite existing CSVs.
This is a plan, NOT measured data. A new combined-controls runner is required.
"""
import csv
import itertools
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "phase1c_experiment_plan.csv"
CPU_PCTS = (20, 50, 80)
RTT_MS = (10, 50, 150)
BANDWIDTH_MBPS = (5, 100)
LOSS_PCTS = (0.0, 2.0)
REPS = 20
PROFILES = ("X25519", "MLKEM768", "X25519MLKEM768")
FIELDS = (
    "scenario_id", "device_id", "cpu_load_target_pct", "cpu_stress_request_pct",
    "memory_ceiling_mib", "rtt_target_ms", "bandwidth_target_mbps",
    "packet_loss_target_pct", "repetitions_per_profile", "dataset_split",
    "experiment_scope", "execution_order",
)


def build():
    rows = []
    for index, (cpu, rtt, bandwidth, loss) in enumerate(
        itertools.product(CPU_PCTS, RTT_MS, BANDWIDTH_MBPS, LOSS_PCTS), 1
    ):
        rows.append({
            "scenario_id": f"C{index:03d}",
            "device_id": "LAPTOP01",
            "cpu_load_target_pct": cpu,
            # Starting calibration only; must validate measured host CPU for each context.
            "cpu_stress_request_pct": cpu,
            "memory_ceiling_mib": 4096,
            "rtt_target_ms": rtt,
            "bandwidth_target_mbps": bandwidth,
            "packet_loss_target_pct": loss,
            "repetitions_per_profile": REPS,
            "dataset_split": "development",
            "experiment_scope": "combined_cpu_and_network_proposed",
            "execution_order": 0,
        })
    rng = random.Random(20261108)
    # Hold out two combinations from every CPU stratum; no context leaks across splits.
    for cpu in CPU_PCTS:
        population = [r for r in rows if r["cpu_load_target_pct"] == cpu]
        for r in rng.sample(population, 2):
            r["dataset_split"] = "held_out_context"
    shuffled = rows.copy()
    rng.shuffle(shuffled)
    for position, row in enumerate(shuffled, start=1):
        row["execution_order"] = position
    return rows


def main():
    rows = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Plan saved: {OUTPUT}")
    print(f"Contexts: {len(rows)} (development: 30; held-out contexts: 6)")
    print(f"Planned TLS measurements: {len(rows) * len(PROFILES) * REPS:,}")
    print("WARNING: Plan only; existing runners cannot apply combined CPU + network controls.")
    print("Memory ceiling is constant and is NOT a memory-pressure experiment.")


if __name__ == "__main__":
    main()
