#!/bin/sh
# Prepares the public demo: simulated warehouse, demo contracts, read-only demo user, then runs the simulator.
set -eu
cd "$(dirname "$0")/.."
if ! python demo/simulator.py status >/dev/null 2>&1; then
  python demo/simulator.py init
fi
nullwake apply demo/contracts --actor demo-bootstrap
printf '%s\n' "$NULLWAKE_DEMO_PASSWORD" | nullwake user "$NULLWAKE_DEMO_USER" --role viewer --name "Demo visitor" --password-stdin
exec python demo/simulator.py run
