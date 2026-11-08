#!/usr/bin/env python3
"""CAACCS / FYP Phase 1B exploratory analysis.

Run from your FYP repository root:
    python3 phase1b_analyze.py

Uses the selected, frozen 330-row dataset and creates CSV summaries, plots,
upper-tail flags, and a Markdown report in data/phase1b_analysis/.

Requirements: Python 3 + matplotlib (python3 -m pip install matplotlib if needed).
No ML training, hidden data deletion, or changes to input files.

For rendering the committed 33-cell aggregate summary instead (no row-level
checks):
    python3 phase1b_analyze.py --summary-csv data/phase1b_full_summary_33.csv
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
# no math imports needed
from pathlib import Path
import statistics
import sys

PROFILES = ("X25519", "MLKEM768", "X25519MLKEM768")
SCENARIOS = tuple(f"S{i:03d}" for i in range(1, 12))
SCENARIO_DETAILS = {
    "S001": "baseline / 10 ms RTT / 100 Mbps / 0% loss",
    "S002": "50% observed CPU target / 4 GiB memory ceiling",
    "S003": "80% observed CPU target / 4 GiB memory ceiling",
    "S004": "20% observed CPU target / 2 GiB memory ceiling",
    "S005": "20% observed CPU target / 1 GiB memory ceiling",
    "S006": "50 ms RTT target",
    "S007": "150 ms RTT target",
    "S008": "25 Mbps configured rate",
    "S009": "5 Mbps configured rate",
    "S010": "0.5% target loss",
    "S011": "2% target loss",
}


def read_raw(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"scenario_id", "profile", "repetition", "elapsed_ms",
                    "success", "negotiated_group", "measurement_scope"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"Missing required raw fields: {required - set(reader.fieldnames or [])}")
        rows = list(reader)
    if len(rows) != 330:
        raise ValueError(f"Expected 330 selected rows, found {len(rows)}")
    observed_keys = Counter()
    groups = defaultdict(list)
    failures = Counter()
    scope_counts = Counter()
    for n, row in enumerate(rows, 2):
        sid, p = row["scenario_id"], row["profile"]
        if sid not in SCENARIOS or p not in PROFILES:
            raise ValueError(f"Unexpected scenario/profile on CSV line {n}: {sid}, {p}")
        try:
            rep = int(row["repetition"])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Bad repetition on CSV line {n}") from exc
        observed_keys[(sid, p, rep)] += 1
        scope_counts[row["measurement_scope"]] += 1
        good = row["success"].strip().lower() == "true"
        if good and row["negotiated_group"] != p:
            raise ValueError(f"TLS group mismatch on CSV line {n}")
        if not good:
            failures[(sid, p)] += 1
            continue
        try:
            elapsed = float(row["elapsed_ms"])
        except ValueError as exc:
            raise ValueError(f"Bad elapsed_ms on CSV line {n}") from exc
        if not 0 < elapsed < float("inf"):
            raise ValueError(f"Nonpositive/nonfinite elapsed_ms on CSV line {n}")
        groups[(sid, p)].append(elapsed)
    expected = {(sid, p, rep): 1 for sid in SCENARIOS for p in PROFILES
                for rep in range(1, 11)}
    if dict(observed_keys) != expected:
        raise ValueError("Missing/duplicated scenario,profile,repetition combination")
    result = []
    for sid in SCENARIOS:
        for p in PROFILES:
            samples = sorted(groups[(sid, p)])
            if not samples:
                raise ValueError(f"No successful measurements for {sid}, {p}")
            mean = statistics.mean(samples)
            std = statistics.stdev(samples) if len(samples) > 1 else 0.0
            result.append({
                "scenario_id": sid,
                "profile": p,
                "successes": len(samples),
                "failures": failures[(sid, p)],
                "mean_elapsed_ms": mean,
                "median_elapsed_ms": statistics.median(samples),
                "stdev_elapsed_ms": std,
                "cv_pct": 100.0 * std / mean,
                "min_elapsed_ms": samples[0],
                "max_elapsed_ms": samples[-1],
            })
    return result, len(rows), scope_counts


def read_committed_summary(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        data = list(csv.DictReader(fh))
    numeric = ("mean_elapsed_ms", "median_elapsed_ms", "stdev_elapsed_ms",
               "cv_pct", "min_elapsed_ms", "max_elapsed_ms")
    result = []
    for row in data:
        cleaned = {k: row[k] for k in ("scenario_id", "profile")}
        for k in numeric:
            cleaned[k] = float(row[k])
        for k in ("successes", "failures"):
            cleaned[k] = int(row[k])
        result.append(cleaned)
    keys = {(r["scenario_id"], r["profile"]) for r in result}
    if len(result) != 33 or keys != {(sid, p) for sid in SCENARIOS for p in PROFILES}:
        raise ValueError("Summary must contain exactly 33 scenario-profile cells")
    return result, sum(r["successes"] + r["failures"] for r in result), None


def to_lookup(summary):
    return {(r["scenario_id"], r["profile"]): r for r in summary}


def output_csv(path: Path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (round(row[k], 3) if isinstance(row.get(k), float) else row.get(k, ""))
                             for k in fields})


def draw_plots(summary, outdir: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("PLOTS SKIPPED: install matplotlib using 'python3 -m pip install matplotlib'.")
        return []
    by = to_lookup(summary)
    produced = []

    def save(filename):
        plt.tight_layout()
        path = outdir / filename
        plt.savefig(path, dpi=180, bbox_inches="tight")
        plt.close()
        produced.append(path)

    # Plot 1: RTT effects at fixed 100 Mbps and zero target loss.
    fig, ax = plt.subplots(figsize=(8, 4.8))
    rtt_sids = ("S001", "S006", "S007")
    x = [10, 50, 150]
    for profile in PROFILES:
        ax.plot(x, [by[(sid, profile)]["median_elapsed_ms"] for sid in rtt_sids],
                marker="o", linewidth=2, label=profile)
    ax.set(xlabel="Target RTT (ms)", ylabel="Median process elapsed (ms)",
           title="RTT effect — process time, not isolated TLS handshake")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.2)
    save("01_rtt_vs_process_time.png")

    # Plot 2: low RTT network scenarios, where profile differences are visible.
    low_sids = ("S001", "S008", "S009", "S010", "S011")
    fig, ax = plt.subplots(figsize=(10, 4.8))
    xs = range(len(low_sids))
    offsets = (-0.24, 0, 0.24)
    for j, profile in enumerate(PROFILES):
        ax.bar([i + offsets[j] for i in xs],
               [by[(sid, profile)]["median_elapsed_ms"] for sid in low_sids],
               width=0.24, label=profile)
    ax.set_xticks(list(xs), ["S001\nbaseline", "S008\n25 Mbps", "S009\n5 Mbps",
                                 "S010\n0.5% loss", "S011\n2% loss"])
    ax.set(ylabel="Median process elapsed (ms)", title="Network settings — medians")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.2)
    save("02_network_medians.png")

    # Plot 3: four CPU contention / memory-ceiling settings.
    sid_order = ("S002", "S003", "S004", "S005")
    fig, ax = plt.subplots(figsize=(9.5, 4.8))
    xs = range(len(sid_order))
    for j, profile in enumerate(PROFILES):
        ax.bar([i + offsets[j] for i in xs],
               [by[(sid, profile)]["median_elapsed_ms"] for sid in sid_order],
               width=0.24, label=profile)
    ax.set_xticks(list(xs), ["S002\n50% CPU, 4 GiB", "S003\n80% CPU, 4 GiB",
                                 "S004\n20% CPU, 2 GiB", "S005\n20% CPU, 1 GiB"])
    ax.set(ylabel="Median process elapsed (ms)",
           title="Resource scenarios — shared-host CPU load, memory ceilings")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.2)
    save("03_resource_medians.png")

    # Plot 4: worst variability cells, highlighting heavy tails.
    biggest = sorted(summary, key=lambda r: r["cv_pct"], reverse=True)[:9]
    fig, ax = plt.subplots(figsize=(9, 5))
    labels = [f"{r['scenario_id']} · {r['profile']}" for r in biggest][::-1]
    values = [r["cv_pct"] for r in biggest][::-1]
    ax.barh(labels, values)
    ax.set(xlabel="Coefficient of variation (%)", title="Largest observed timing variability")
    ax.grid(axis="x", alpha=0.2)
    save("04_high_variability_cells.png")
    return produced


def create_report(summary, n, outdir, mode):
    by = to_lookup(summary)
    winners = []
    for sid in SCENARIOS:
        p = min(PROFILES, key=lambda p: by[(sid, p)]["median_elapsed_ms"])
        winners.append(p)
    winner_counts = Counter(winners)
    xbase = by[("S001", "X25519")]["median_elapsed_ms"]
    x150 = by[("S007", "X25519")]["median_elapsed_ms"]
    x5 = by[("S009", "X25519")]["median_elapsed_ms"]
    h5 = by[("S009", "X25519MLKEM768")]["median_elapsed_ms"]
    loss_h = by[("S011", "X25519MLKEM768")]
    flagged = []
    for r in summary:
        ratio = r["max_elapsed_ms"] / r["median_elapsed_ms"]
        if r["cv_pct"] >= 25 or ratio >= 1.5:
            flagged.append({**r, "max_to_median_ratio": ratio})
    flagged.sort(key=lambda r: r["max_to_median_ratio"], reverse=True)
    fields = ["scenario_id", "profile", "successes", "failures", "median_elapsed_ms",
              "mean_elapsed_ms", "stdev_elapsed_ms", "cv_pct", "min_elapsed_ms",
              "max_elapsed_ms", "max_to_median_ratio"]
    output_csv(outdir / "upper_tail_flags.csv", flagged, fields)

    text = [
        "# CAACCS — Phase 1B exploratory analysis",
        "",
        f"**Basis:** {n} selected TLS trials across 11 scenarios and three profiles; "
        f"10 repetitions per scenario/profile cell. **Mode:** {mode}.",
        "",
        "## Key findings",
        "",
        f"- Network delay dominates the whole-process measurement: X25519 median was "
        f"{xbase:.3f} ms at the baseline and {x150:.3f} ms at S007 (150 ms target RTT). "
        "This is not isolated key-establishment time.",
        f"- Under S009 (configured 5 Mbps), the hybrid median was {h5:.3f} ms, "
        f"versus {x5:.3f} ms for X25519 (difference {h5-x5:.3f} ms). "
        "Configured bandwidth was not re-measured as actual throughput during these TLS trials.",
        f"- Under S011 (2% target packet loss), one hybrid trial took "
        f"{loss_h['max_elapsed_ms']:.3f} ms versus the cell median of "
        f"{loss_h['median_elapsed_ms']:.3f} ms; CV was {loss_h['cv_pct']:.1f}%. "
        "This may reflect retransmission/scheduling, but the cause was not established.",
        "- Descriptive lowest-median profile counts across 11 scenarios: " +
        ", ".join(f"{p}: {winner_counts[p]}" for p in PROFILES) +
        ". This does NOT establish a safe or statistically significant winner.",
        "",
        "## All 11 scenario medians (milliseconds)",
        "",
        "| Scenario | X25519 | MLKEM768 | Hybrid | Context |",
        "|---|---:|---:|---:|---|",
    ]
    for sid in SCENARIOS:
        medians = [by[(sid, p)]["median_elapsed_ms"] for p in PROFILES]
        text.append(f"| {sid} | {medians[0]:.3f} | {medians[1]:.3f} | "
                    f"{medians[2]:.3f} | {SCENARIO_DETAILS[sid]} |")
    text += [
        "",
        "## Tail behavior and reliability",
        "",
        "The companion `upper_tail_flags.csv` contains **descriptive flags**, not exclusions: "
        "a cell is flagged when CV is at least 25% or maximum exceeds 1.5× its median. "
        "With only 10 repetitions, confidence in tails is low. Keep all original trials.",
        "",
        "## Methodological limits",
        "",
        "1. The timing field (`elapsed_ms`) includes OpenSSL process launch, TCP, TLS, and shutdown. "
        "Do not describe it as pure TLS handshake or KEM latency.",
        "2. S001 and S006–S011 are network-only; reported CPU/memory target values were not enforced there.",
        "3. S002–S005 measure shared-host CPU contention and verified **memory ceilings**. "
        "Observed memory usage was far below those ceilings, so memory pressure was not demonstrated.",
        "4. Network data were collected in separate batches and scenario order was not randomized. "
        "Within scenarios, crypto-profile trial order was randomized; systematic drift is still possible.",
        "5. One WSL host/device, 11 contexts, 10 repetitions per cell. "
        "Do not claim broad real-world generalizability or strong statistical rankings.",
        "6. Each run used a curated TLS group. The ML decision framework must first apply "
        "security and peer-compatibility constraints; the cheapest profile is not automatically permitted.",
        "",
        "## Priority dataset expansion (designed, not brute-force)",
        "",
        "**Priority 1 — better outcome measurement.** Separately capture TLS handshake completion "
        "rather than only the subprocess wall clock. Log process startup overhead, failures/timeouts, "
        "OpenSSL version, host load, and trial timestamps. Validate measurements against an independent method.",
        "",
        "**Priority 2 — meaningful new contexts.** Add combined RTT × bandwidth × packet loss × CPU "
        "conditions via a balanced subset (for example, 24–36 deliberately selected scenarios). "
        "Vary test order across batches. Use measured available memory/pressure rather than only cgroup ceilings.",
        "",
        "**Priority 3 — breadth and repeatability.** Aim initially for about 20 repetitions per "
        "scenario-profile cell across independent sessions/days, and a second hardware class if available. "
        "An illustrative next campaign of 24–36 contexts × 3 profiles × 20 repetitions = "
        "1,440–2,160 connections; this is a planning example, not a required minimum.",
        "",
        "**Priority 4 — valid adaptive evaluation.** Define the runtime-available feature schema "
        "(network measurements, device state, requirements, peer capability). Do **not** use "
        "post-choice measurements like elapsed time or negotiated group as input features. "
        "Split model evaluation by held-out scenarios and ideally held-out devices, not random individual "
        "rows that share the exact same context. Build fixed-profile and rule-based baselines first.",
        "",
        "### Security guardrail",
        "",
        "Profiles must be filtered for the scenario's security/policy/compatibility requirements "
        "before any performance ranking. In particular, if quantum-resistant key establishment "
        "is mandatory, a fast classical-only group must not be recommended.",
        "",
        "---",
        "These findings are exploratory and should not be presented as a production-ready "
        "adaptive cryptography model or a statistically proven algorithm ranking.",
    ]
    path = outdir / "phase1b_findings.md"
    path.write_text("\n".join(text) + "\n", encoding="utf-8")
    return path, flagged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/phase1b_full_330.csv"))
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--outdir", type=Path, default=Path("data/phase1b_analysis"))
    args = parser.parse_args()
    if args.summary_csv:
        summary, total, scopes = read_committed_summary(args.summary_csv)
        mode = f"committed aggregate summary ({args.summary_csv.name})"
    else:
        summary, total, scopes = read_raw(args.input)
        mode = f"verified raw dataset ({args.input})"
    args.outdir.mkdir(parents=True, exist_ok=True)
    fields = ["scenario_id", "profile", "successes", "failures", "median_elapsed_ms",
              "mean_elapsed_ms", "stdev_elapsed_ms", "cv_pct", "min_elapsed_ms", "max_elapsed_ms"]
    output_csv(args.outdir / "phase1b_analysis_33.csv", summary, fields)
    plots = draw_plots(summary, args.outdir)
    report_path, tail = create_report(summary, total, args.outdir, mode)
    print(f"Analyzed {total} rows; {len(summary)} scenario/profile groups.")
    if scopes:
        print("Measurement scopes:", dict(scopes))
    print(f"Heuristic upper-tail flags: {len(tail)} cells (observations retained).")
    print("Report:", report_path)
    for p in plots:
        print("Plot:", p)
    print("Inputs preserved. No machine learning performed.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, csv.Error) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
