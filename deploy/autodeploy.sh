#!/usr/bin/env bash
# Auto-deploy watcher: polls GitHub for new commits and, when the remote is ahead,
# pulls the changes and restarts the agent. This gives you "push to GitHub -> server
# updates itself 24/7". Runs as its own systemd service (idkbro-autodeploy.service).
#
# It NEVER touches .env or the data/ folder (his brain), so secrets + memory survive.
set -euo pipefail

REPO_DIR="${IDKBRO_DIR:-$HOME/idkbro}"
BRANCH="${IDKBRO_BRANCH:-main}"
INTERVAL="${IDKBRO_POLL_SECONDS:-60}"

cd "$REPO_DIR"

echo "[autodeploy] watching $REPO_DIR on origin/$BRANCH every ${INTERVAL}s"

while true; do
  git fetch origin "$BRANCH" --quiet || { sleep "$INTERVAL"; continue; }
  LOCAL=$(git rev-parse HEAD)
  REMOTE=$(git rev-parse "origin/$BRANCH")

  if [ "$LOCAL" != "$REMOTE" ]; then
    echo "[autodeploy] new commit $REMOTE, updating…"
    # Keep local .env + data; only fast-forward code.
    git reset --hard "origin/$BRANCH"
    # Reinstall deps in case requirements changed.
    if [ -f backend/requirements.txt ]; then
      ./backend/.venv/bin/pip install -q -r backend/requirements.txt || true
    fi
    # Restart the agent service (systemd). Ignore if not managed by systemd.
    sudo systemctl restart idkbro 2>/dev/null || echo "[autodeploy] restart the agent manually"
    echo "[autodeploy] updated to $REMOTE"
  fi
  sleep "$INTERVAL"
done
