#!/usr/bin/env bash
# Run the full engine analysis on an EC2 instance, then power the instance off
# so it doesn't keep billing. Run after scripts/ec2-setup.sh, from the repo folder:
#
#   nohup bash scripts/ec2-run.sh > analyse.log 2>&1 &
#   tail -f analyse.log
#
# The instance shuts down whether the run succeeds or fails. With the default
# EC2 "shutdown behaviour = Stop" the disk (and evals.parquet on it) survives:
# start the instance again, copy the file off, then terminate it.
#
# Safety net: the instance also shuts down after MAX_MINUTES regardless, in
# case the run hangs. Cancel that with `sudo shutdown -c`.
set -uo pipefail
cd "$(dirname "$0")/.."

MAX_MINUTES="${MAX_MINUTES:-360}"
sudo shutdown -h "+${MAX_MINUTES}" "chess-analysis safety-net shutdown" 2>/dev/null

echo "== $(date -u +%FT%TZ) starting analyse on $(nproc) CPUs (safety-net shutdown in ${MAX_MINUTES} min)"
.venv/bin/chess-analysis analyse "$@"
status=$?
echo "== $(date -u +%FT%TZ) analyse exited with status ${status}; shutting down in 1 minute"
sync
sudo shutdown -h +1 "chess-analysis finished"
exit "${status}"
