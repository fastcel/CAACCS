# Phase 1C dataset specification — development snapshot v1

**Scope:** CAACCS Phase 1C, 36 planned CPU × RTT × bandwidth × packet-loss contexts, one device `LAPTOP01`, three TLS 1.3 key-establishment groups. This file freezes **dataset interpretation and preprocessing only**, not a model, a security decision policy, or a held-out evaluation. Raw data already collected on `phase-1c` through commit `4ba1151ec9d13c740a70a2af323ccdb85d38be2a`.

## Dataset status and partition

- Plan: 3 CPU targets (20%, 50%, 80%) × 3 RTT targets (10, 50, 150 ms) × 2 configured bandwidths (5, 100 Mbps) × 2 configured losses (0%, 2%) = **36 contexts**.
- **30 development contexts** collected and validated: 60 measured connections per context, 20 per group, hence **1,800 measured attempts**; three additional warm-up connections per scenario excluded.
- **6 held-out contexts** (`C006`, `C008`, `C016`, `C017`, `C031`, `C033`) remain uncollected at the development snapshot. They must not enter data exploration, preprocessing design, cross-validation, training, or hyperparameter tuning.
- The complete 2,160-trial dataset **does not exist yet**. Do not claim Phase 1C's 36-scenario collection is complete until the six held-out batches have been measured and validated.
- Partition by **entire scenario**, never random trial/row split. Repeated trials in the same context are not 20 independent contexts.

## Inputs, provenance, and immutable originals

- Plan: `data/phase1c_experiment_plan.csv`, including immutable `dataset_split` and `execution_order`.
- For each full development batch, preserve the original triplet: `data/phase1c_raw/phase1c_planned_probe_Cxxx_<timestamp>.csv`, corresponding `_cpu_samples.csv`, and `_metadata.json`.
- Runner: `phase1c_combined_runner_v3.py`; probe: `phase1c_tls_probe.c`; original validation: `phase1c_validate_v2.py`.
- Execute `python3 phase1c_validate_v2.py --all` before finalization. Its `missing scenarios remain pending` report is expected until held-out collection.
- The versioned finalizer `phase1c_finalize_development_v1.py` reads **development raw data only**, asserts uniqueness and structural consistency, checks metadata and CPU samples, writes SHA-256 source checksums, and creates a reproducible derived table. It never edits raw measurements. If the v1 generated output already exists but differs, it errors rather than silently overwriting.
- A development snapshot is not a claim that all data are ready for final testing. Do not overwrite or quietly rerun accepted raw batches. Record discrepancies as a new explicitly versioned amendment.

## Raw trial fields: meaning and measurement boundaries

| Field(s) | Meaning | Important limitation |
|---|---|---|
| `run_id`, `scenario_id`, `device_id`, `profile`, `repetition`, `trial_order_in_scenario`, `timestamp_utc` | Trial identity, group, and order/time | Randomized execution order is not a random train/test split |
| `run_type`, `dataset_split`, `measurement_method`, `measurement_scope` | Provenance and partition | Use full (`planned`) probe-method runs; do not mix smoke or legacy measurements |
| `cpu_load_target_pct`, `cpu_stress_requested_pct`, `cpu_workers`, `cpu_method`, `cpu_control_applied` | Configured shared-host CPU-stress condition | This is **not** isolated server CPU load |
| `cpu_observed_during_trial_pct_approx` | Approximate trial-associated sampled CPU | Not a direct per-profile CPU cost |
| `memory_limit_mb`, `memory_control_applied`, `memory_max_verified_bytes` | Memory cgroup ceiling (4096 MiB) and verification | **Not** a RAM-pressure experiment; do not call this measured per-profile peak RAM |
| `rtt_target_ms`, `rtt_observed_ms` | Target RTT and ping-validated RTT, milliseconds | Observed validation RTT is ping-context measurement |
| `bandwidth_target_mbps`, `bandwidth_measurement_status` | Configured traffic shaping and its measurement status | **Throughput was not measured** |
| `packet_loss_target_pct`, `netem_loss_per_direction_pct` | Requested end-to-end loss target and applied per-direction shaping | Do not conflate per-direction netem value with observed end-to-end loss |
| `validation_ping_packets_sent`, `validation_ping_packets_received`, `validation_ping_loss_pct` | Ping validation sample counts and observed loss | Ping loss is not TLS failure rate; 0%-loss checks use 20 packets, 2%-loss checks 500 |
| `tcp_ms` | Time for TCP connect(), in milliseconds | **Separate** from TLS handshake time |
| `tls_handshake_ms` | Time for SSL_connect(), in milliseconds | Includes network, crypto, certificate/protocol work, and scheduling; **not crypto-only** |
| `elapsed_ms` | Probe subprocess wall duration, milliseconds | Not interchangeable with `tcp_ms` or `tls_handshake_ms` |
| `cpu_time_ms`, `peak_memory_mb`, `bytes_read`, `bytes_written` | Optional legacy/resource/byte fields in common raw schema | Blank values mean **not measured**; do not treat as zero or invent values |
| `negotiated_group`, `success`, `error_reason` | Negotiated group, TLS connection outcome, failure explanation | Count failed trials in the denominator; require reason for failed attempts |

CPU samples CSV: `timestamp_utc` and `cpu_utilization_pct`, sampled approximately once per second. Metadata JSON preserves plan, CPU monitor mean/sample count, RTT/loss preflight, cgroup ceiling and peak, measurement method, and binary probe hash.

## Frozen development preprocessing (v1)

1. Accept only scenario IDs whose **plan** `dataset_split` is exactly `development`; only single full-batch `planned` probe file per scenario. Exclude warmups and smoke files entirely.
2. Preserve **all 60 trial rows** per scenario, including failures and outliers; do not delete, winsorize, or impute. Check 20 unique repetitions per cryptographic group.
3. For success-rate calculations, use all attempts: `successes/20` per profile/scenario. Never infer high reliability from 20 successes under configured packet loss.
4. Compute `tcp_ms` and `tls_handshake_ms` summaries from **successful connections only**. If none succeed, their latency summaries remain blank; keep the failure counts and error reasons in raw files.
5. For successful TLS timing values per group, compute **median**, **max**, and **p95 nearest rank** (sorted observation at `ceil(0.95 * successful_count)`); do not confuse p95 with a measured probability of failure.
6. The 90-row summary consists of one observation **per scenario × group** (30 × 3). The raw 1,800-row data and CPU samples remain the authoritative observations.
7. Use controlled values (CPU target, RTT target, configured bandwidth, configured loss) as scenario features in later modeling. Treat observed CPU/RTT/ping loss as **validation diagnostics**, not independent workload interventions or algorithm-specific resource costs.
8. No standardized scaling, missing-value imputation, feature-selection, performance labeling, or model fitting is locked here; those belong to the separately frozen development/evaluation protocol. Fit any statistical transformations on training folds only, not on held-out contexts.
9. Leave held-out contexts **sealed until model and evaluation procedure are fixed**. When finally collected, store them in the same raw schema and validate the full experiment, but do not use them to revise the v1 preprocessing procedure based on observed outcomes.

## Derived development outputs

`data/phase1c_dataset_v1/development_profile_summary.csv`: 90 rows, with the following fields:

| Derived column | Definition |
|---|---|
| `scenario_id`, `dataset_split`, `device_id` | Identity and partition |
| `cpu_load_target_pct`, `memory_ceiling_mib`, `rtt_target_ms`, `bandwidth_target_mbps`, `packet_loss_target_pct` | Planned configuration (not measured per-profile costs) |
| `cpu_over_run_pct`, `rtt_observed_ms`, `validation_ping_loss_pct` | Run-wide validation diagnostics, repeated for each profile row |
| `profile` | TLS key-establishment group |
| `attempts`, `successes`, `failures`, `success_fraction` | Counts and descriptive completion fraction |
| `tcp_median_success_ms` | Median of successful `tcp_ms` values |
| `tls_median_success_ms` | Median of successful `tls_handshake_ms` values |
| `tls_p95_nearest_rank_success_ms` | Nearest-rank p95 across successful TLS handshakes |
| `tls_max_success_ms` | Maximum successful TLS handshake time |

`development_source_manifest.csv`: relative filenames, byte lengths, SHA-256 checksums of original plan, runner, validator, probe source, dataset spec, finalizer, and raw/CPU/metadata files.

`development_audit_report.txt`: deterministic audit summary.

## Interpretation and exclusions

The benchmark measures **TLS handshake timing with network effects**, TCP connection timing, run-wide CPU contention and monitoring, configured bandwidth/loss, ping RTT/loss validation, and a verified 4096 MiB ceiling. It **does not** measure throughput, actual memory pressure, energy, peer interoperability beyond supported test configurations, pure KEM execution times, or algorithm-isolated CPU/memory costs. These limitations must be retained in papers and reports.

The exploratory ranking and bootstrap analyses are **not** frozen training labels or evidence that ML outperforms a static baseline. Final model/policy decisions and held-out evaluation procedures must be recorded separately before held-out data are consulted.
