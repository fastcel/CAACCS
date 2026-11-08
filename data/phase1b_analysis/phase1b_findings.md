# CAACCS — Phase 1B exploratory analysis

**Basis:** 330 selected TLS trials across 11 scenarios and three profiles; 10 repetitions per scenario/profile cell. **Mode:** verified raw dataset (data/phase1b_full_330.csv).

## Key findings

- Network delay dominates the whole-process measurement: X25519 median was 43.114 ms at the baseline and 472.366 ms at S007 (150 ms target RTT). This is not isolated key-establishment time.
- Under S009 (configured 5 Mbps), the hybrid median was 50.555 ms, versus 46.285 ms for X25519 (difference 4.270 ms). Configured bandwidth was not re-measured as actual throughput during these TLS trials.
- Under S011 (2% target packet loss), one hybrid trial took 1142.692 ms versus the cell median of 44.739 ms; CV was 224.6%. This may reflect retransmission/scheduling, but the cause was not established.
- Descriptive lowest-median profile counts across 11 scenarios: X25519: 7, MLKEM768: 4, X25519MLKEM768: 0. This does NOT establish a safe or statistically significant winner.

## All 11 scenario medians (milliseconds)

| Scenario | X25519 | MLKEM768 | Hybrid | Context |
|---|---:|---:|---:|---|
| S001 | 43.114 | 42.806 | 43.236 | baseline / 10 ms RTT / 100 Mbps / 0% loss |
| S002 | 55.838 | 55.975 | 56.744 | 50% observed CPU target / 4 GiB memory ceiling |
| S003 | 65.730 | 66.372 | 80.258 | 80% observed CPU target / 4 GiB memory ceiling |
| S004 | 53.622 | 54.334 | 54.064 | 20% observed CPU target / 2 GiB memory ceiling |
| S005 | 56.123 | 55.398 | 55.650 | 20% observed CPU target / 1 GiB memory ceiling |
| S006 | 164.701 | 165.857 | 165.435 | 50 ms RTT target |
| S007 | 472.366 | 468.841 | 470.722 | 150 ms RTT target |
| S008 | 43.212 | 44.505 | 44.111 | 25 Mbps configured rate |
| S009 | 46.285 | 49.790 | 50.555 | 5 Mbps configured rate |
| S010 | 43.439 | 43.145 | 43.165 | 0.5% target loss |
| S011 | 43.334 | 43.981 | 44.739 | 2% target loss |

## Tail behavior and reliability

The companion `upper_tail_flags.csv` contains **descriptive flags**, not exclusions: a cell is flagged when CV is at least 25% or maximum exceeds 1.5× its median. With only 10 repetitions, confidence in tails is low. Keep all original trials.

## Methodological limits

1. The timing field (`elapsed_ms`) includes OpenSSL process launch, TCP, TLS, and shutdown. Do not describe it as pure TLS handshake or KEM latency.
2. S001 and S006–S011 are network-only; reported CPU/memory target values were not enforced there.
3. S002–S005 measure shared-host CPU contention and verified **memory ceilings**. Observed memory usage was far below those ceilings, so memory pressure was not demonstrated.
4. Network data were collected in separate batches and scenario order was not randomized. Within scenarios, crypto-profile trial order was randomized; systematic drift is still possible.
5. One WSL host/device, 11 contexts, 10 repetitions per cell. Do not claim broad real-world generalizability or strong statistical rankings.
6. Each run used a curated TLS group. The ML decision framework must first apply security and peer-compatibility constraints; the cheapest profile is not automatically permitted.

## Priority dataset expansion (designed, not brute-force)

**Priority 1 — better outcome measurement.** Separately capture TLS handshake completion rather than only the subprocess wall clock. Log process startup overhead, failures/timeouts, OpenSSL version, host load, and trial timestamps. Validate measurements against an independent method.

**Priority 2 — meaningful new contexts.** Add combined RTT × bandwidth × packet loss × CPU conditions via a balanced subset (for example, 24–36 deliberately selected scenarios). Vary test order across batches. Use measured available memory/pressure rather than only cgroup ceilings.

**Priority 3 — breadth and repeatability.** Aim initially for about 20 repetitions per scenario-profile cell across independent sessions/days, and a second hardware class if available. An illustrative next campaign of 24–36 contexts × 3 profiles × 20 repetitions = 1,440–2,160 connections; this is a planning example, not a required minimum.

**Priority 4 — valid adaptive evaluation.** Define the runtime-available feature schema (network measurements, device state, requirements, peer capability). Do **not** use post-choice measurements like elapsed time or negotiated group as input features. Split model evaluation by held-out scenarios and ideally held-out devices, not random individual rows that share the exact same context. Build fixed-profile and rule-based baselines first.

### Security guardrail

Profiles must be filtered for the scenario's security/policy/compatibility requirements before any performance ranking. In particular, if quantum-resistant key establishment is mandatory, a fast classical-only group must not be recommended.

---
These findings are exploratory and should not be presented as a production-ready adaptive cryptography model or a statistically proven algorithm ranking.
