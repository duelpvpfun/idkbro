#!/usr/bin/env bash
# One-shot VPS installer for idkbro.
# Run on a fresh Ubuntu server as a normal sudo user:
#   curl -fsSL https://raw.githubusercontent.com/duelpvpfun/idkbro/main/deploy/setup.sh | bash
# ...or after cloning:  bash deploy/setup.sh
#
# It: installs python, clones the repo (if needed), makes a venv, installs deps,
# restores his learned brain from the memory-backup branch (if present), and sets up
# both systemd services (the agent + the auto-deploy watcher).
# You still paste your API keys into .env at the end (secrets never live in git).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/duelpvpfun/idkbro.git}"
DIR="${IDKBRO_DIR:-$HOME/idkbro}"

echo "==> installing prerequisites"
sudo apt-get update -y -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git

echo "==> getting the code at $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only || true
else
  git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"

echo "==> python venv + deps"
python3 -m venv backend/.venv
./backend/.venv/bin/pip install -q --upgrade pip
./backend/.venv/bin/pip install -q -r backend/requirements.txt

echo "==> restoring his brain from the memory-backup branch (if any)"
mkdir -p backend/data
if git fetch origin memory-backup --quiet 2>/dev/null; then
  if git cat-file -e origin/memory-backup:idkbro.db 2>/dev/null; then
    git show origin/memory-backup:idkbro.db > backend/data/idkbro.db
    echo "    restored idkbro.db ($(du -h backend/data/idkbro.db | cut -f1))"
  fi
else
  echo "    no backup branch yet, starting fresh"
fi

echo "==> .env"
if [ ! -f backend/.env ]; then
  cp backend/.env.example backend/.env
  echo "    created backend/.env from template - YOU MUST ADD YOUR KEYS"
fi

echo "==> systemd services"
USER_NAME="$(whoami)"
sed "s#/home/idkbro/idkbro#$DIR#g; s#User=idkbro#User=$USER_NAME#g" deploy/idkbro.service \
  | sudo tee /etc/systemd/system/idkbro.service >/dev/null
sed "s#/home/idkbro/idkbro#$DIR#g; s#User=idkbro#User=$USER_NAME#g" deploy/idkbro-autodeploy.service \
  | sudo tee /etc/systemd/system/idkbro-autodeploy.service >/dev/null
sudo systemctl daemon-reload

cat <<EOF

==================== ALMOST DONE ====================
1) Add your API keys:      nano $DIR/backend/.env
2) Start the agent:        sudo systemctl enable --now idkbro
3) Start auto-deploy:      sudo systemctl enable --now idkbro-autodeploy
4) Watch it:               journalctl -u idkbro -f
   Dashboard (safe):       ssh -L 8000:localhost:8000 $USER_NAME@<server-ip>  then open http://localhost:8000
=====================================================
EOF
