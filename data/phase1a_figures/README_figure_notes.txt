FYP PHASE 1A — PILOT FIGURE NOTES
Source: fyp_phase1a_pilot_99_20261019_175608_612890.csv

FIGURE 1: RTT pilot, S001/S006/S007. Target round-trip time: 10, 50, 150 ms.
FIGURE 2: Bandwidth pilot, S001/S008/S009. Configured egress rate: 100, 25, 5 Mbps.
FIGURE 3: Packet loss pilot, S001/S010/S011. Configured round-trip target: 0, 0.5, 2%.
FIGURE 4: Resource pilots, S002–S005. Observed benchmark-wide CPU utilization (%):
  S002: 49.47; S003: 82.69;
  S004: 16.99; S005: 22.7.
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
