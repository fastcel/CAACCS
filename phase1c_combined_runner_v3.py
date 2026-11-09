#!/usr/bin/env python3
"""Phase 1C CAACCS combined CPU/network benchmark (RTT validation v3).

Usage (from the FYP project root):
    python3 phase1c_combined_runner_v3.py --scenario C023 --check

Smoke test (requires configured veth namespace, running TLS s_server, cgroup v2,
             stress-ng and passwordless sudo after `sudo -v`):
    sudo -v
    sudo systemd-run --scope --same-dir -p MemoryMax=4096M \
      /usr/sbin/runuser -u "$(id -un)" -- /bin/sh -c \
      'sudo -v && exec /usr/bin/python3 phase1c_combined_runner_v3.py --scenario C023 --smoke'

A smoke test performs 1 measured connection per group, not a full dataset run.
A full planned run requires explicit --full, after successful validation.

Default --timing probe: calls the separately compiled ./phase1c_tls_probe,
recording tcp_ms and tls_handshake_ms from connect() and SSL_connect().
Each C probe is a NEW process and TLS connection (not connection reuse).
The runner also records subprocess wall time in elapsed_ms, a separate metric.

Optional --timing legacy reuses the original OpenSSL s_client process-timing
method for diagnostics; TLS-only timings are blank for legacy mode. Never mix
timing methods without filtering measurement_method.

TLS handshake time still includes network RTT, cryptographic work, certificate
verification and scheduling. It is NOT crypto-only. CPU contention is
shared-host (server included); MemoryMax is a ceiling, not memory pressure.
Bandwidth is configured via netem, NOT benchmarked via iperf3 in this run.

Phase 1C RTT validation uses max(5 ms, 5% of target) as a declared
engineering tolerance and 20 pings for no-loss scenarios (500 for loss).
The observed RTT and tolerance are retained in metadata. This tolerance is
NOT evidence of precise emulated RTT or a substitute for iperf3 throughput.
Original Phase 1B RTT validation remains unchanged.

IMPORTANT: Replaces root netem qdiscs on veth-client and fyp-server:veth-server.
On normal exit/Ctrl+C it REMOVES those qdiscs, even if they existed before.
Do not Ctrl+Z or run concurrent experiments on those interfaces.
Original Phase 1A/1B files are never modified.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time

import network_conditions_pilot as net
import cpu_memory_scenarios_pilot as cpu

ROOT = Path(__file__).resolve().parent
PLAN = ROOT / 'data' / 'phase1c_experiment_plan.csv'
PROBE = ROOT / 'phase1c_tls_probe'
PROFILES = net.PROFILES
FIELDS = [
    'run_id', 'scenario_id', 'device_id', 'profile', 'repetition',
    'trial_order_in_scenario', 'timestamp_utc', 'run_type', 'dataset_split',
    'cpu_load_target_pct', 'cpu_stress_requested_pct', 'cpu_workers',
    'cpu_method', 'cpu_control_applied', 'cpu_observed_during_trial_pct_approx',
    'memory_limit_mb', 'memory_control_applied', 'memory_max_verified_bytes',
    'rtt_target_ms', 'rtt_observed_ms', 'validation_ping_packets_sent',
    'validation_ping_packets_received', 'validation_ping_loss_pct',
    'bandwidth_target_mbps', 'bandwidth_measurement_status',
    'packet_loss_target_pct', 'netem_loss_per_direction_pct',
    'measurement_method', 'tcp_ms', 'tls_handshake_ms',
    'elapsed_ms', 'cpu_time_ms', 'peak_memory_mb', 'bytes_read', 'bytes_written',
    'negotiated_group', 'success', 'error_reason', 'measurement_scope',
]


def load_plan(scenario_id):
    if not PLAN.is_file():
        raise FileNotFoundError(f'Missing {PLAN}. Run phase1c_make_plan.py first.')
    required = {
        'scenario_id', 'device_id', 'cpu_load_target_pct',
        'cpu_stress_request_pct', 'memory_ceiling_mib', 'rtt_target_ms',
        'bandwidth_target_mbps', 'packet_loss_target_pct',
        'repetitions_per_profile', 'dataset_split', 'experiment_scope',
        'execution_order',
    }
    with PLAN.open(newline='', encoding='utf-8-sig') as f:
        rdr = csv.DictReader(f)
        if not required.issubset(rdr.fieldnames or []):
            raise ValueError(f'Plan missing columns: {sorted(required - set(rdr.fieldnames or []))}')
        records = list(rdr)
    if len(records) != 36 or len({r['scenario_id'] for r in records}) != 36:
        raise ValueError('Expected 36 unique planned scenario IDs.')
    if len({r['execution_order'] for r in records}) != 36:
        raise ValueError('Expected 36 unique execution orders.')
    for r in records:
        if r['experiment_scope'] != 'combined_cpu_and_network_proposed':
            raise ValueError(f'Unexpected scope in {r["scenario_id"]}')
        if r['dataset_split'] not in {'development', 'held_out_context'}:
            raise ValueError(f'Unexpected split in {r["scenario_id"]}')
        if int(r['repetitions_per_profile']) != 20:
            raise ValueError(f'Expected 20 planned repetitions in {r["scenario_id"]}')
        if int(r['memory_ceiling_mib']) != 4096:
            raise ValueError(f'Unexpected memory ceiling in {r["scenario_id"]}')
        for key in ('cpu_load_target_pct', 'cpu_stress_request_pct',
                    'rtt_target_ms', 'bandwidth_target_mbps', 'packet_loss_target_pct'):
            if not math.isfinite(float(r[key])):
                raise ValueError(f'Invalid {key} in {r["scenario_id"]}')
        if int(float(r['cpu_stress_request_pct'])) not in (20, 50, 80):
            raise ValueError('Unexpected CPU stress request')
        if float(r['rtt_target_ms']) not in (10, 50, 150):
            raise ValueError('Unexpected RTT')
        if float(r['bandwidth_target_mbps']) not in (5, 100):
            raise ValueError('Unexpected bandwidth')
        if float(r['packet_loss_target_pct']) not in (0, 2):
            raise ValueError('Unexpected packet loss')
    combo = {(r['cpu_stress_request_pct'], r['rtt_target_ms'],
              r['bandwidth_target_mbps'], r['packet_loss_target_pct']) for r in records}
    if len(combo) != 36:
        raise ValueError('Expected 36 unique CPU/RTT/bandwidth/loss combinations.')
    matches = [r for r in records if r['scenario_id'] == scenario_id]
    if len(matches) != 1:
        raise ValueError(f'Unknown or duplicate scenario {scenario_id}')
    return matches[0]


def validate_phase1c_ping(row):
    """Validate observed RTT without Phase 1B's fixed ±5 ms cutoff.

    Phase 1C uses a declared ±max(5 ms, 5% of target) engineering tolerance.
    This is a validation rule for a noisy WSL testbed, not a claim that netem
    precisely produces the target or a measurement of available throughput.
    """
    target_ms = float(row['rtt_target_ms'])
    planned_loss_pct = float(row['packet_loss_target_pct'])
    tolerance_ms = max(5.0, 0.05 * target_ms)
    count = 500 if planned_loss_pct > 0 else 20
    timeout_s = 100 if count == 500 else 25
    command = ['ping', '-n', '-q', '-c', str(count), '-i', '0.1',
               '-W', '1', net.TARGET_IP]
    result = subprocess.run(command, capture_output=True, text=True,
                            env=net.ENV, timeout=timeout_s)
    output = result.stdout + result.stderr
    counts = re.search(r'(\d+) packets transmitted,\s*(\d+) received,\s*([\d.]+)% packet loss', output)
    rtt = re.search(r'(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = [\d.]+/([\d.]+)/', output)
    if not counts or not rtt:
        raise RuntimeError(f'{row["scenario_id"]}: unable to parse ping validation: {output[-1000:]}')
    actual_ms = float(rtt.group(1))
    error_ms = round(actual_ms - target_ms, 3)
    summary = {
        'rtt_observed_ms': round(actual_ms, 3),
        'validation_ping_packets_sent': int(counts.group(1)),
        'validation_ping_packets_received': int(counts.group(2)),
        'validation_ping_loss_pct': float(counts.group(3)),
        'rtt_validation_tolerance_ms': round(tolerance_ms, 3),
        'rtt_validation_error_ms': error_ms,
        'rtt_validation_rule': 'absolute_error_le_max_5ms_or_5pct_target',
    }
    if not math.isfinite(actual_ms) or abs(error_ms) > tolerance_ms:
        raise RuntimeError(
            f'{row["scenario_id"]}: observed RTT {actual_ms:.3f} ms vs target '
            f'{target_ms:.3f} ms; difference {error_ms:+.3f} ms exceeds declared '
            f'tolerance ±{tolerance_ms:.3f} ms (not accepting this batch)'
        )
    return summary


def probe_tls(profile):
    """One new TLS connection; retain failures and mark unavailable metrics blank.

    elapsed_ms measures the probe subprocess's wall clock time from Python;
    tcp_ms and tls_handshake_ms come from the C CLOCK_MONOTONIC timers.
    """
    command = [str(PROBE), profile, str(net.CERT)]
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   env=net.ENV, timeout=20)
        output = completed.stdout.strip()
        stderr = completed.stderr.strip()
        rc = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b'').decode(errors='replace') if isinstance(exc.stdout, bytes) else (exc.stdout or '')
        stderr = (exc.stderr or b'').decode(errors='replace') if isinstance(exc.stderr, bytes) else (exc.stderr or '')
        rc = None
        timed_out = True
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    fields = {}
    for part in output.split():
        if '=' in part:
            key, value = part.split('=', 1)
            fields[key] = value
    authenticated = (fields.get('profile') == profile and
                     fields.get('negotiated_group') == profile and
                     fields.get('certificate_verified') == 'true' and
                     fields.get('session_reused') == '0' and
                     fields.get('version') == 'TLSv1.3' and
                     fields.get('cipher') == 'TLS_AES_128_GCM_SHA256')
    try:
        tcp_ms = float(fields['tcp_ms'])
        tls_ms = float(fields['tls_handshake_ms'])
        good_times = all(math.isfinite(v) and v >= 0 for v in (tcp_ms, tls_ms))
    except (KeyError, ValueError):
        tcp_ms, tls_ms, good_times = '', '', False
    success = rc == 0 and authenticated and good_times
    err = '' if success else ('timeout after 20s' if timed_out else
                             f'probe exit={rc}; ' + (stderr or output or 'missing/invalid fields')[-500:])
    return {
        'tcp_ms': tcp_ms if success else '',
        'tls_handshake_ms': tls_ms if success else '',
        'elapsed_ms': wall_ms,
        'cpu_time_ms': '', 'peak_memory_mb': '',
        'bytes_read': '', 'bytes_written': '',
        'negotiated_group': fields.get('negotiated_group', ''),
        'success': success, 'error_reason': err,
    }


def run(args):
    plan = load_plan(args.scenario)
    reps = 1 if args.smoke else int(plan['repetitions_per_profile'])
    bench = probe_tls if args.timing == 'probe' else net.bench_tls
    sid = plan['scenario_id']
    memory_mib = int(plan['memory_ceiling_mib'])
    memory_bytes = memory_mib * 1024 * 1024
    cpu_request = int(plan['cpu_stress_request_pct'])
    worker_count = len(os.sched_getaffinity(0))
    mode = 'smoke' if args.smoke else 'planned'
    print(f'{sid} | {mode} | timing={args.timing} | split={plan["dataset_split"]} | order={plan["execution_order"]}')
    print(f'CPU target/request={plan["cpu_load_target_pct"]}%/{cpu_request}%; '
          f'memory ceiling={memory_mib} MiB; RTT={plan["rtt_target_ms"]} ms; '
          f'bandwidth={plan["bandwidth_target_mbps"]} Mbps; '
          f'loss={plan["packet_loss_target_pct"]}%; CPU workers={worker_count}')
    print(f'{reps} measured repetitions/profile => {3 * reps} trials; '
          '3 warm-up connections not counted')
    print('Timing method:', 'C probe tcp_ms + tls_handshake_ms (separate)'
          if args.timing == 'probe' else 'legacy OpenSSL process elapsed only')
    print('Bandwidth is configured but throughput is NOT measured here.')
    if args.check:
        print('CHECK ONLY: no sudo, cgroup, network changes, stress or TLS calls.')
        return
    if os.geteuid() == 0:
        raise RuntimeError('Run as normal user inside systemd scope via runuser, not root.')
    required = ['/usr/bin/stress-ng']
    required += [str(PROBE)] if args.timing == 'probe' else [net.OPENSSL, net.GNU_TIME]
    for name in required:
        if not Path(name).is_file():
            raise FileNotFoundError(f'Missing {name}')
    if args.timing == 'probe' and not os.access(PROBE, os.X_OK):
        raise RuntimeError(f'{PROBE} is not executable; compile phase1c_tls_probe.c first')
    if not net.CERT.is_file():
        raise FileNotFoundError(f'Missing TLS certificate {net.CERT}')
    cgroup = cpu.validate_memory_scope(sid, memory_bytes, memory_mib)
    print(f'Verified cgroup memory.max: {memory_bytes} bytes')
    net.run_checked(['sudo', '-n', 'true'])
    preflight = bench('X25519')
    if not preflight['success']:
        raise RuntimeError('TLS preflight failed before shaping: ' + preflight['error_reason'])
    print('TLS preflight passed.')

    out_dir = ROOT / 'data' / ('phase1c_smoke' if args.smoke else 'phase1c_raw')
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    stem = f'phase1c_{mode}_{args.timing}_{sid}_{stamp}'
    csv_path = out_dir / (stem + '.csv')
    samples_path = out_dir / (stem + '_cpu_samples.csv')
    info_path = out_dir / (stem + '_metadata.json')

    trial_count = 0
    successes = 0
    samples = []
    monitor = None
    stress = None
    shaping_touched = False
    error = None
    observations = None
    applied_loss = None
    start_utc = datetime.now(timezone.utc).isoformat()
    try:
        shaping_touched = True   # cleanup even if first tc replace succeeds and second fails
        # Reconfigure only the FYP veth pair. Any earlier root qdiscs get replaced.
        applied_loss = net.tc_apply(plan)
        stress = subprocess.Popen(
            ['/usr/bin/stress-ng', '--cpu', str(worker_count),
             '--cpu-method', cpu.CPU_METHOD, '--cpu-load', str(cpu_request),
             '--timeout', '600s'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(cpu.WARMUP_SEC)
        if stress.poll() is not None:
            raise RuntimeError('CPU stress worker stopped before measurements')
        print('CPU stress stabilized; checking RTT/loss under CPU contention...')
        observations = validate_phase1c_ping(plan)
        print(f'Ping: RTT={observations["rtt_observed_ms"]} ms, '
              f'loss={observations["validation_ping_loss_pct"]}% '
              f'({observations["validation_ping_packets_sent"]} packets); '
              f'RTT tolerance=±{observations["rtt_validation_tolerance_ms"]} ms')
        monitor = cpu.CpuMonitor()
        monitor.start()
        for profile in PROFILES:
            warmup = bench(profile)
            if not warmup['success']:
                raise RuntimeError(f'Warm-up TLS failed for {profile}: {warmup["error_reason"]}')
        trials = [(p, i) for p in PROFILES for i in range(1, reps + 1)]
        random.Random(20261108 + int(sid[1:])).shuffle(trials)
        with csv_path.open('x', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            fh.flush()
            for order, (profile, rep) in enumerate(trials, 1):
                if stress.poll() is not None:
                    raise RuntimeError('CPU stress stopped before all trials finished')
                before = cpu.read_cpu_ticks()
                result = bench(profile)
                after = cpu.read_cpu_ticks()
                approx_cpu = cpu.busy_pct(before, after)
                trial_count += 1
                successes += int(result['success'])
                writer.writerow({
                    'run_id': f'C1R{order:05d}',
                    'scenario_id': sid,
                    'device_id': plan['device_id'],
                    'profile': profile,
                    'repetition': rep,
                    'trial_order_in_scenario': order,
                    'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                    'run_type': mode,
                    'dataset_split': plan['dataset_split'],
                    'cpu_load_target_pct': plan['cpu_load_target_pct'],
                    'cpu_stress_requested_pct': cpu_request,
                    'cpu_workers': worker_count,
                    'cpu_method': cpu.CPU_METHOD,
                    'cpu_control_applied': True,
                    'cpu_observed_during_trial_pct_approx': approx_cpu,
                    'memory_limit_mb': memory_mib,
                    'memory_control_applied': True,
                    'memory_max_verified_bytes': memory_bytes,
                    'rtt_target_ms': plan['rtt_target_ms'],
                    'rtt_observed_ms': observations['rtt_observed_ms'],
                    'validation_ping_packets_sent': observations['validation_ping_packets_sent'],
                    'validation_ping_packets_received': observations['validation_ping_packets_received'],
                    'validation_ping_loss_pct': observations['validation_ping_loss_pct'],
                    'bandwidth_target_mbps': plan['bandwidth_target_mbps'],
                    'bandwidth_measurement_status': 'configured_not_measured_this_run',
                    'packet_loss_target_pct': plan['packet_loss_target_pct'],
                    'netem_loss_per_direction_pct': applied_loss,
                    'measurement_scope': 'shared_host_cpu_contention_plus_netem_memory_ceiling',
                    'measurement_method': 'openssl_ssl_connect_probe_v1' if args.timing == 'probe'
                                          else 'openssl_s_client_process_v1',
                    'tcp_ms': '', 'tls_handshake_ms': '',
                    **result,
                })
                fh.flush()   # retain completed rows if interrupted
                timing_label = (f'TCP={result["tcp_ms"]} ms | TLS={result["tls_handshake_ms"]} ms'
                                if args.timing == 'probe' else f'process={result["elapsed_ms"]} ms')
                print(f'{sid} | {profile} | rep {rep} | {timing_label} | '
                      f'success={result["success"]}', flush=True)
                if not result['success']:
                    print('  failure:', result['error_reason'], flush=True)
                if order < len(trials):
                    time.sleep(cpu.GAP_BETWEEN_TRIALS_SEC)
        print(f'Completed {trial_count} trials ({successes} successful).')
    except BaseException as exc:
        error = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if monitor is not None:
            monitor.stop()
            samples = monitor.samples
        cpu.kill_stress(stress)
        avg_cpu = (round(sum(s['cpu_utilization_pct'] for s in samples)/len(samples), 2)
                   if samples else None)
        peak = cgroup / 'memory.peak'
        peak_mib = round(int(peak.read_text()) / 1024**2, 3) if peak.is_file() else None
        if samples:
            with samples_path.open('x', newline='', encoding='utf-8') as fh:
                writer = csv.DictWriter(fh, fieldnames=['timestamp_utc', 'cpu_utilization_pct'])
                writer.writeheader()
                writer.writerows(samples)
        meta = {
            'scenario_id': sid, 'run_type': mode, 'dataset_split': plan['dataset_split'],
            'measurement_method': 'openssl_ssl_connect_probe_v1' if args.timing == 'probe'
                                  else 'openssl_s_client_process_v1',
            'probe_executable_sha256': (hashlib.sha256(PROBE.read_bytes()).hexdigest()
                                        if args.timing == 'probe' else None),
            'plan': plan, 'runner_validation_version': 'phase1c_rtt_tolerance_v3',
            'run_started_utc': start_utc,
            'run_finished_utc': datetime.now(timezone.utc).isoformat(),
            'intended_repetitions_per_profile': reps,
            'measured_trials': trial_count, 'successes': successes,
            'cpu_requested_pct': cpu_request, 'cpu_over_run_pct': avg_cpu,
            'cpu_monitor_sample_count': len(samples), 'cpu_workers': worker_count,
            'memory_max_verified_bytes': memory_bytes, 'cgroup_peak_mib': peak_mib,
            'netem_per_direction_loss_pct': applied_loss,
            'network_validation': observations,
            'measurement_note': ('tcp_ms=connect(); tls_handshake_ms=SSL_connect(); '
                                 'elapsed_ms=Python probe subprocess wall time; '
                                 'TLS time includes network+crypto+verification+scheduling'
                                 if args.timing == 'probe' else
                                 'elapsed_ms=OpenSSL s_client process startup+TCP/TLS'),
            'resource_note': 'Shared host CPU contention and cgroup memory ceiling, not RAM pressure',
            'bandwidth_note': 'Configured, not measured in this run',
            'raw_csv': csv_path.name if csv_path.exists() else None,
            'cpu_samples_csv': samples_path.name if samples_path.exists() else None,
            'error': error,
        }
        with info_path.open('x', encoding='utf-8') as fh:
            json.dump(meta, fh, indent=2)
            fh.write('\n')
        if shaping_touched:
            net.cleanup_tc()
            print('Removed temporary shaping from FYP veth pair.')
        print('CSV:', csv_path if csv_path.exists() else 'No trials recorded')
        print('Metadata:', info_path)
        if avg_cpu is not None:
            print(f'Average monitored CPU: {avg_cpu}% | Cgroup peak memory: {peak_mib} MiB')
        if error:
            print('Run issue:', error, file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--scenario', required=True, help='Plan scenario ID, e.g. C001')
    p.add_argument('--check', action='store_true', help='Preview only: no experiments or system changes')
    p.add_argument('--smoke', action='store_true', help='Only one measured TLS trial/profile; saved separately')
    p.add_argument('--full', action='store_true', help='Explicitly authorize the 20 repetitions/profile full scenario')
    p.add_argument('--timing', choices=('probe', 'legacy'), default='probe',
                   help='Default: native TLS timing probe. Legacy retains previous timing method.')
    args = p.parse_args()
    if sum((args.check, args.smoke, args.full)) != 1:
        p.error('Choose exactly one: --check, --smoke, or --full')
    run(args)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted; previously completed CSV rows are preserved.', file=sys.stderr)
        sys.exit(130)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
