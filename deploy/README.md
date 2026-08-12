# Running idkbro 24/7

The app is a single Python process with a local SQLite database (`backend/data/idkbro.db`).
"Local database" just means the DB file lives next to the app wherever it runs — on a
server that's the server's disk, so it runs 24/7 with your PC off. All learning (playbook,
lessons, trades, wallet reputations, watchlist, open positions) is saved there and survives
restarts.

---

## 1. Testing now (Codespaces / VS Code)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add your keys
python -m app.main
```

Open http://localhost:8000. This stops when you close the machine — fine for testing and
bug-fixing, not for the multi-day run.

---

## 2. The 24/7 run on a VPS

Any cheap VPS works (Hetzner ~€4/mo, DigitalOcean/Vultr/Contabo ~$5-6/mo). Two options.

### Option A — Docker (simplest)

On the server:

```bash
git clone <your-repo-url> idkbro
cd idkbro/backend
cp .env.example .env        # paste your keys (nano .env)
docker compose up -d --build
```

- Dashboard: `http://<server-ip>:8000`
- Logs: `docker compose logs -f`
- Update after code changes: `git pull && docker compose up -d --build`
- Stop: `docker compose down`

`restart: unless-stopped` + the `./data` volume mean it auto-restarts on crash/reboot and
keeps its brain.

### Option B — systemd (no Docker)

```bash
git clone <your-repo-url> ~/idkbro
cd ~/idkbro/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # paste your keys
sudo cp ../deploy/idkbro.service /etc/systemd/system/idkbro.service
# edit User/paths in the unit file if your username isn't "idkbro"
sudo systemctl daemon-reload
sudo systemctl enable --now idkbro
```

- Status: `sudo systemctl status idkbro`
- Live logs: `journalctl -u idkbro -f`
- Restart after update: `git pull && sudo systemctl restart idkbro`

---

## 3. Auto-deploy: push to GitHub → server updates itself

A server does NOT pull from GitHub on its own. This watcher does: it polls GitHub every 60s
and, when you push a new commit, pulls it, reinstalls deps, and restarts the agent. Your
`.env` and `data/` (his brain) are never touched.

```bash
# after the systemd setup above (Option B), also install the watcher:
sudo cp ~/idkbro/deploy/idkbro-autodeploy.service /etc/systemd/system/
# edit User/paths in the file if your username isn't "idkbro"
sudo systemctl daemon-reload
sudo systemctl enable --now idkbro-autodeploy
```

- Watcher logs: `journalctl -u idkbro-autodeploy -f`
- Now your workflow is just: **edit here → push to GitHub → the server picks it up within a
  minute and restarts automatically.**

For the private repo, the server needs read access. Easiest: a GitHub **deploy key** (a
read-only SSH key added to the repo's Settings → Deploy keys), or clone with a
fine-grained personal access token.

> Docker users: the same watcher works, just change the restart line in `autodeploy.sh`
> to `docker compose up -d --build` instead of `systemctl restart idkbro`.

---

## 4. Reaching the dashboard safely

The dashboard has no login, so don't expose port 8000 to the whole internet long-term.
Easiest safe options:

- **SSH tunnel** (nothing public): `ssh -L 8000:localhost:8000 user@server-ip` then open
  http://localhost:8000 locally.
- Or put it behind a reverse proxy (Caddy/nginx) with basic auth later.

---

## 4. Backups

Everything it has learned is in `backend/data/idkbro.db`. To back up:

```bash
cp backend/data/idkbro.db ~/idkbro-backup-$(date +%F).db
```

That's the whole brain — copy it and you can move the agent to another machine anytime.
