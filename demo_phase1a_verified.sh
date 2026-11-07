#!/usr/bin/env bash
# CAACS / FYP-I: reproducible Phase 1A evidence demo.
# Runs the EXISTING network_conditions_pilot.py. Does not overwrite the frozen 99-row dataset.
# Preconditions: FYP veth/namespace configured; OpenSSL 3.5 TLS server running at 10.10.0.2:4433.
set -Eeuo pipefail

cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")"

if [[ ! -f network_conditions_pilot.py || ! -f data/scenarios.csv ]]; then
  echo 'ERROR: Put this script in your FYP project root alongside network_conditions_pilot.py.' >&2
  exit 1
fi
if [[ ! -x /opt/openssl-3.5/bin/openssl ]]; then
  echo 'ERROR: OpenSSL 3.5 binary was not found at /opt/openssl-3.5/bin/openssl.' >&2
  exit 1
fi
if [[ ! -f "$HOME/fyp-tls-test/server.crt" ]]; then
  echo 'ERROR: Lab server certificate missing from ~/fyp-tls-test/server.crt.' >&2
  exit 1
fi
if ! command -v ip >/dev/null || ! command -v ss >/dev/null || ! command -v sudo >/dev/null; then
  echo 'ERROR: ip, ss, and sudo are required.' >&2
  exit 1
fi

echo '== CAACS Phase 1A live demonstration =='
echo 'Scenario S001 · 10 ms RTT target · 100 Mbps configured · 0% loss target'
echo 'Three key-establishment groups · one measurement per group'

echo 'Checking lab environment...'
sudo -v
if ! sudo -n ip netns list | grep -q '^fyp-server'; then
  echo 'ERROR: fyp-server namespace missing. Restore the previously working veth/namespace setup.' >&2
  exit 1
fi
if ! ip link show dev veth-client >/dev/null 2>&1; then
  echo 'ERROR: veth-client missing. Restore the previously working veth/namespace setup.' >&2
  exit 1
fi
if ! sudo -n ip netns exec fyp-server ss -ltn | grep -q '10\.10\.0\.2:4433'; then
  echo 'ERROR: TLS server is not listening at 10.10.0.2:4433 in fyp-server.' >&2
  echo 'Start the existing OpenSSL 3.5 s_server in a separate Ubuntu terminal.' >&2
  exit 1
fi

LD_LIBRARY_PATH="/opt/openssl-3.5/lib64:${LD_LIBRARY_PATH:-}" \
  /opt/openssl-3.5/bin/openssl version

mkdir -p data/midterm_demo_logs
LOG="data/midterm_demo_logs/demo_S001_$(date -u +%Y%m%d_%H%M%S).log"
echo "Logging terminal evidence to $LOG"

# The pilot script itself applies and removes tc netem on FYP virtual interfaces.
python3 -u network_conditions_pilot.py --scenarios S001 --repetitions 1 | tee "$LOG"

# Read the actual timestamped path printed by the source runner; never guess a filename.
CSV_PATH="$(sed -n 's/^Results saved to: //p' "$LOG" | tail -n 1)"
if [[ -z "$CSV_PATH" || ! -f "$CSV_PATH" ]]; then
  echo 'ERROR: No output CSV confirmed by the original benchmark runner.' >&2
  exit 1
fi

python3 - "$CSV_PATH" <<'PY'
import csv
from pathlib import Path
import sys

path = Path(sys.argv[1])
with path.open(newline='', encoding='utf-8-sig') as fh:
    rows = list(csv.DictReader(fh))
expected = {'X25519', 'MLKEM768', 'X25519MLKEM768'}
errors = []
if len(rows) != 3:
    errors.append(f'expected 3 measured rows, got {len(rows)}')
found = [r.get('profile', '') for r in rows]
if set(found) != expected or len(found) != len(set(found)):
    errors.append(f'profiles missing or duplicated: {found!r}')
for r in rows:
    profile = r.get('profile', '')
    if r.get('scenario_id') != 'S001':
        errors.append(f'{profile}: wrong scenario ID')
    if r.get('success', '').strip().lower() != 'true':
        errors.append(f'{profile}: TLS success flag is not true')
    if r.get('negotiated_group', '') != profile:
        errors.append(f'{profile}: actual TLS negotiated group mismatch')
    if r.get('measurement_scope', '') != 'network_only_pilot':
        errors.append(f'{profile}: measurement scope mismatch')
    try:
        if float(r.get('elapsed_ms', '')) <= 0:
            errors.append(f'{profile}: invalid elapsed time')
    except ValueError:
        errors.append(f'{profile}: missing/non-numeric elapsed time')

print('\n== New-run CSV validation ==')
for r in sorted(rows, key=lambda x:x.get('profile', '')):
    print(f"{r.get('profile',''):17s} | negotiated={r.get('negotiated_group',''):17s} "
          f" elapsed_ms={r.get('elapsed_ms','')} success={r.get('success','')}")
print(f'Output: {path}')
if errors:
    print('RESULT: FAILED')
    for error in errors:
        print(' -', error)
    sys.exit(1)
print('RESULT: PASSED — 3 distinct profiles, 3 successful TLS 1.3 negotiations, matching groups')
print('Note: These are process elapsed-time measurements, not isolated TLS handshake times.')
print('Note: This is a fresh demo CSV; do not merge into the finalized 99-row pilot data.')
PY

printf '\nNext: open the frozen Phase 1A RTT figure (data/phase1a_figures/fig_01_rtt.png)\n'
printf 'Original research validation: data/fyp_phase1a_validation_20261019_175608_612890.txt\n'
