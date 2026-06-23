#!/bin/bash
# Larbild deploy — runs ON Sibbis. git-PULL based (never rsync).
#
# Pulls the latest larbild master from the VM350 bare repo into the live
# checkout, (re)installs deps, and restarts larbild.service. Idempotent: a
# sentinel records the last successfully-deployed SHA so a no-op pull skips
# the venv/restart churn unless --force is given.
#
# WHY git-pull here but git-pull+rsync in imagegen (hq-0vte, deliberate
# divergence): larbild RUNS FROM its git checkout, so a pull deploys it.
# imagegen's runtime dir is separate from its checkout (repo carries
# docs/assets that must not land in the runtime) and needs the rsync subset.
# Same-host, different app layouts — not drift.
#
# Deploys larbild.service (the authored-SVG library UI/APIs). The diffusion
# render worker (larbild-worker.service) was removed when larbild committed
# permanently to authored SVG content — there is no render path to deploy.
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

# Re-exec ourselves so bash re-parses the updated script on disk.
# Without this, any change to this script's logic (e.g. a new systemctl
# restart line) would only land on the NEXT deploy — one deploy late.
# The DEPLOY_REEXEC env flag prevents an infinite loop.
if [ "${DEPLOY_REEXEC:-}" != "1" ]; then
  export DEPLOY_REEXEC=1
  exec bash "$0" "$@"
fi

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

# Remove the retired render-worker unit + its redis drop-in if a prior deploy
# installed them (idempotent cleanup — larbild is now SVG-only, no render path).
if systemctl list-unit-files larbild-worker.service >/dev/null 2>&1 \
   && [ -f /etc/systemd/system/larbild-worker.service ]; then
  sudo systemctl disable --now larbild-worker.service >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/larbild-worker.service
  sudo rm -f /etc/systemd/system/larbild.service.d/10-redis.conf
  sudo systemctl daemon-reload
  echo "Removed retired larbild-worker.service + redis drop-in."
fi

echo "Restarting larbild.service..."
sudo systemctl restart larbild.service
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
