#!/usr/bin/env python3
"""FYP Phase 1A: make descriptive plots from the 99-record mixed-control PILOT.

Run from the FYP folder:
    python3 plot_phase1a_pilot.py

Optional explicit input:
    python3 plot_phase1a_pilot.py --input data/fyp_phase1a_pilot_99_20261019_175608_612890.csv

Produces PNG and SVG figures in data/phase1a_figures/; never edits source CSVs.
The plots are descriptive ONLY, not tests of cryptographic superiority.
"""
import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
import sys

try:
    import matplotlib
    matplotlib.use("Agg")  # works in WSL2 with no GUI/display
    import matplotlib.pyplot as plt
except ImportError:
    print("Missing matplotlib. Install it with: sudo apt install -y python3-matplotlib", file=sys.stderr)
    raise SystemExit(2)

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DEFAULT_INPUT = DATA / "fyp_phase1a_pilot_99_20261019_175608_612890.csv"
PROFILES = ("X25519", "MLKEM768", "X25519MLKEM768")
PROFILE_LABELS = {"X25519": "X25519 (classical)",
                  "MLKEM768": "ML-KEM-768 (PQC)",
                  "X25519MLKEM768": "X25519 + ML-KEM-768 (hybrid)"}
SCENARIOS = tuple(f"S{i:03d}" for i in range(1, 12))


def load_rows(input_file):
    if not input_file.is_file():
        raise ValueError(f"Cannot find merged dataset: {input_file}")
    with input_file.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        required = {"scenario_id", "profile", "repetition", "elapsed_ms",
                    "success", "measurement_scope", "cpu_control_applied",
                    "memory_control_applied", "cpu_observed_over_benchmark_pct"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing columns: {sorted(missing)}")
        rows = list(reader)
    if len(rows) != 99:
        raise ValueError(f"Expected 99 pilot records, found {len(rows)}")
    counts = Counter((r["scenario_id"], r["profile"]) for r in rows)
    if set(counts) != {(sid, p) for sid in SCENARIOS for p in PROFILES} or any(n != 3 for n in counts.values()):
        raise ValueError("Expected exactly three records per each of 33 scenario-profile cells")
    for r in rows:
        if r["success"].lower() != "true":
            raise ValueError("An unsuccessful record was found; don't silently omit it")
        try:
            r["elapsed_value"] = float(r["elapsed_ms"])
        except ValueError as e:
            raise ValueError(f"Non-numeric elapsed time: {r['elapsed_ms']}") from e
    for r in rows:
        if r["scenario_id"] in ("S001", "S006", "S007", "S008", "S009", "S010", "S011"):
            if r["cpu_control_applied"].lower() != "false" or r["memory_control_applied"].lower() != "false":
                raise ValueError("Network-only pilot control flags unexpected")
        else:
            if r["cpu_control_applied"].lower() != "true" or r["memory_control_applied"].lower() != "true":
                raise ValueError("Resource pilot control flags unexpected")
    return rows


def summarize(rows):
    grouped = defaultdict(list)
    for r in rows:
        grouped[(r["scenario_id"], r["profile"])].append(r["elapsed_value"])
    return grouped


def style_axes(ax, title, caption):
    ax.set_title(title, loc="left", fontsize=13, weight="bold", pad=13)
    ax.set_ylabel("Process elapsed time (ms)")
    ax.grid(axis="y", alpha=0.25)
    ax.set_axisbelow(True)
    ax.text(0, -0.25, caption, transform=ax.transAxes, fontsize=8,
            color="0.4", ha="left", va="top", wrap=True)


def categorical_plot(grouped, scenarios, labels, title, caption, destination):
    fig, ax = plt.subplots(figsize=(9.8, 5.6), constrained_layout=False)
    x = list(range(len(scenarios)))
    # Matplotlib default palette; raw points reveal that medians use only 3 trials.
    for profile in PROFILES:
        medians = [median(grouped[(sid, profile)]) for sid in scenarios]
        (line,) = ax.plot(x, medians, marker="o", linewidth=2,
                          label=PROFILE_LABELS[profile])
        for xx, sid in zip(x, scenarios):
            values = grouped[(sid, profile)]
            ax.scatter([xx] * len(values), values, marker="_", s=85,
                       color=line.get_color(), alpha=0.6, zorder=3)
    ax.set_xticks(x, labels)
    ax.set_xlim(-0.25, len(x) - 0.75)
    ax.legend(fontsize=9, loc="upper left", frameon=False)
    style_axes(ax, title, caption)
    fig.subplots_adjust(left=0.12, right=0.97, top=0.86, bottom=0.22)
    for ext in ("png", "svg"):
        fig.savefig(destination.with_suffix("." + ext), dpi=190, facecolor="white")
    plt.close(fig)


def write_notes(out_dir, source_file, rows):
    cpu_by_scenario = {}
    for sid in ("S002", "S003", "S004", "S005"):
        readings = [float(r["cpu_observed_over_benchmark_pct"]) for r in rows
                    if r["scenario_id"] == sid and r["cpu_observed_over_benchmark_pct"]]
        cpu_by_scenario[sid] = round(median(readings), 2) if readings else "not recorded"
    notes = f"""FYP PHASE 1A — PILOT FIGURE NOTES
Source: {source_file.name}

FIGURE 1: RTT pilot, S001/S006/S007. Target round-trip time: 10, 50, 150 ms.
FIGURE 2: Bandwidth pilot, S001/S008/S009. Configured egress rate: 100, 25, 5 Mbps.
FIGURE 3: Packet loss pilot, S001/S010/S011. Configured round-trip target: 0, 0.5, 2%.
FIGURE 4: Resource pilots, S002–S005. Observed benchmark-wide CPU utilization (%):
  S002: {cpu_by_scenario['S002']}; S003: {cpu_by_scenario['S003']};
  S004: {cpu_by_scenario['S004']}; S005: {cpu_by_scenario['S005']}.
  Enforced cgroup memory ceilings: S002=4096 MiB, S003=4096 MiB,
  S004=2048 MiB, S005=1024 MiB. Low memory usage means this is NOT
  a test of actual physical RAM pressure.

PLOT MARKS: connected circles are per-profile MEDIANS; small tick marks are
individual trials (three per scenario/profile). These are descriptive only.

RESEARCH CAUTIONS:
* Network-only S001/S006–S011 did not apply the target CPU/memory controls.
* S002–S005 generated shared-host CPU contention, including the TLS server.
* Measured elapsed_ms covers subprocess startup, TCP connection and TLS work;
  it is NOT isolated cryptographic key-establishment latency.
* Three repetitions per scenario/profile are too few for reliable rankings.
* These figures are suitable as a working Phase 1A pilot demonstration,
  not for a final performance claim or ML training targets.
* These plots use only local measured CSV data; no values are fabricated.
"""
    (out_dir / "README_figure_notes.txt").write_text(notes, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                    help="99-row merged Phase 1A pilot CSV")
    args = ap.parse_args()
    source = args.input.expanduser().resolve()
    rows = load_rows(source)
    grouped = summarize(rows)
    out_dir = DATA / "phase1a_figures"
    out_dir.mkdir(parents=True, exist_ok=True)

    categorical_plot(grouped,
        ["S001", "S006", "S007"], ["10 ms", "50 ms", "150 ms"],
        "1. Effect of round-trip latency — network-only pilot",
        "TLS process elapsed time; 3 trials per profile/setting. Lines join medians; small ticks show individual runs.",
        out_dir / "fig_01_rtt")
    categorical_plot(grouped,
        ["S001", "S008", "S009"], ["100 Mbps", "25 Mbps", "5 Mbps"],
        "2. Effect of configured bandwidth — network-only pilot",
        "Configured throughput limits validated separately with iperf3. Process elapsed time includes TCP and startup overhead.",
        out_dir / "fig_02_bandwidth")
    categorical_plot(grouped,
        ["S001", "S010", "S011"], ["0%", "0.5%", "2%"],
        "3. Effect of configured packet loss — network-only pilot",
        "Observed ping loss varies stochastically. Nine successful trials per scenario do not establish a failure probability.",
        out_dir / "fig_03_packet_loss")
    categorical_plot(grouped,
        ["S002", "S003", "S004", "S005"],
        ["S002\n50%, 4 GiB", "S003\n80%, 4 GiB", "S004\n20%, 2 GiB", "S005\n20%, 1 GiB"],
        "4. CPU contention & memory ceilings — resource pilots",
        "X labels: target CPU %, enforced cgroup memory ceiling. Shared-host contention; no RAM pressure test.",
        out_dir / "fig_04_resource_scenarios")
    write_notes(out_dir, source, rows)
    print("Phase 1A descriptive figures created using:", source)
    for p in sorted(out_dir.iterdir()):
        if p.is_file():
            print(" ", p.relative_to(HERE))
    print("NOTE: pilot graphs, not final statistical comparisons or ML labels.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as err:
        raise SystemExit(f"ERROR: {err}")
