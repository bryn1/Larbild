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

# Install/refresh the systemd units from the repo templates so a FRESH deploy
# brings up the render worker (lb-3z8). The committed docs/*.service templates
# carry a leading "#"-comment header (install hints); drop those leading comment
# lines so only the real [Unit]/[Service] body lands. Idempotent: only
# daemon-reload + enable when the installed unit actually changed.
install_unit() {  # <repo-template> <dest-path>
  local src="$1" dest="$2" tmp
  tmp=$(mktemp)
  # Strip the contiguous leading block of "#" comment lines (the install header).
  sed '0,/^[^#]/{/^#/d}' "$src" > "$tmp"
  if ! sudo cmp -s "$tmp" "$dest" 2>/dev/null; then
    sudo install -D -m 0644 "$tmp" "$dest"
    echo "Installed unit: $dest"
    UNITS_CHANGED=1
  fi
  rm -f "$tmp"
}
UNITS_CHANGED=0
install_unit "$APP/docs/larbild-worker.service" \
  /etc/systemd/system/larbild-worker.service
install_unit "$APP/docs/larbild.service.d-10-redis.conf" \
  /etc/systemd/system/larbild.service.d/10-redis.conf
if [ "$UNITS_CHANGED" -eq 1 ]; then
  sudo systemctl daemon-reload
  sudo systemctl enable larbild-worker.service >/dev/null 2>&1 || true
fi

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
