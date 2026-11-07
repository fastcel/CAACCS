"""Safely combine the two completed network-only FYP pilot CSVs.

Run from the FYP project folder or any directory, provided this script is
stored alongside the project's data/ directory.
"""

import csv
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INPUTS = [
    DATA_DIR / "raw_network_conditions_pilot_20261019_145421_222319.csv",
    DATA_DIR / "raw_network_conditions_pilot_20261019_150902_116438.csv",
]
OUTPUT = DATA_DIR / "raw_network_conditions_pilot_combined_63.csv"
PROFILES = ("X25519", "MLKEM768", "X25519MLKEM768")
SCENARIOS = ("S001", "S006", "S007", "S008", "S009", "S010", "S011")

all_rows = []
expected_header = None
for source, expected_count, allowed_scenarios in zip(
    INPUTS, (54, 9), (set(SCENARIOS) - {"S011"}, {"S011"})
):
    with source.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"Missing CSV header: {source.name}")
        if expected_header is None:
            expected_header = reader.fieldnames
        elif reader.fieldnames != expected_header:
            raise ValueError(f"CSV headers differ: {source.name}")
        rows = list(reader)
    if len(rows) != expected_count:
        raise ValueError(f"Expected {expected_count} rows in {source.name}, got {len(rows)}")
    for row in rows:
        if row["scenario_id"] not in allowed_scenarios:
            raise ValueError(f"Unexpected scenario {row['scenario_id']} in {source.name}")
        all_rows.append({"source_csv": source.name, "original_run_id": row["run_id"], **row})

assert expected_header is not None
counts = Counter((r["scenario_id"], r["profile"]) for r in all_rows)
expected = Counter({(scenario, profile): 3 for scenario in SCENARIOS for profile in PROFILES})
if counts != expected:
    missing = expected - counts
    extra = counts - expected
    raise ValueError(f"Unexpected scenario/profile counts. Missing: {missing}; Extra: {extra}")

for i, row in enumerate(all_rows, start=1):
    row["run_id"] = f"R{i:04d}"

# Preserve the original run ID and source filename for traceability.
fields = ["run_id", "original_run_id", "source_csv"] + [
    field for field in expected_header if field != "run_id"
]
# Exclusive creation prevents overwriting a merged dataset from a previous run.
with OUTPUT.open("x", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    writer.writerows(all_rows)

print("Merged pilot CSV created:", OUTPUT)
print("Total measured records:", len(all_rows))
for scenario in SCENARIOS:
    print(f"  {scenario}: {sum(r['scenario_id'] == scenario for r in all_rows)} records")
print("Reported successful TLS runs:", sum(r["success"].strip().lower() == "true" for r in all_rows))
print("Unique run IDs:", len({r["run_id"] for r in all_rows}))
print("Original CSV files were not modified.")
