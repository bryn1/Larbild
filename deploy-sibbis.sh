#!/bin/bash
# Larbild deploy — runs ON Sibbis. git-PULL based (never rsync).
#
# Pulls the latest larbild master from the VM350 bare repo into the live
# checkout, (re)installs deps, and restarts larbild.service. Idempotent: a
# sentinel records the last successfully-deployed SHA so a no-op pull skips
# the venv/restart churn unless --force is given.
#
# Deploys both larbild.service (UI/APIs) and larbild-worker.service (the render
# worker → ComfyUI on the shared VM100 3090 via render-residency).
#
# Usage:
#   bash deploy-sibbis.sh            # pull + deploy if HEAD changed
#   bash deploy-sibbis.sh --force    # redeploy even if SHA unchanged
set -euo pipefail
APP=/home/lektove/larbild-app
REMOTE=claudecode@192.168.5.231:/home/claudecode/gt/larbild-source.git
SENTINEL=/home/lektove/.larbild-last-deployed-sha
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

cd "$APP"
# Ensure origin points at the VM350 bare repo (git-pull source of truth).
git remote set-url origin "$REMOTE" 2>/dev/null || git remote add origin "$REMOTE"
git fetch --quiet origin master
git reset --hard origin/master
HEAD_SHA=$(git rev-parse HEAD)
echo "HEAD now: $(git log --oneline -1)"

if [ "$FORCE" -eq 0 ] && [ -f "$SENTINEL" ] && [ "$(cat "$SENTINEL")" = "$HEAD_SHA" ]; then
  echo "Already deployed $HEAD_SHA — nothing to do (use --force to redeploy)."
  exit 0
fi

echo "Installing deps..."
[ -d venv ] || python3 -m venv venv
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt

echo "Restarting larbild.service + larbild-worker.service..."
sudo systemctl restart larbild.service
# The render worker is a SEPARATE unit (BRPOP loop → ComfyUI on the shared VM100
# 3090). It MUST be restarted too so render-path code (render_lane / render_worker)
# is picked up — a stale worker would run the old render coordination (hq-jx6g).
sudo systemctl restart larbild-worker.service
sleep 3
# Smoke gate: only write the sentinel if /healthz answers 200.
for i in $(seq 1 15); do
  if curl -fsS http://127.0.0.1:6868/healthz >/dev/null 2>&1; then break; fi
  sleep 1
done
if curl -fsS http://127.0.0.1:6868/healthz >/dev/null 2>&1; then
  echo "$HEAD_SHA" > "$SENTINEL"
  echo "Deployed + healthy: $HEAD_SHA"
else
  echo "ERROR: /healthz did not come up after restart" >&2
  sudo systemctl status larbild.service --no-pager -l | tail -20 >&2
  exit 1
fi
