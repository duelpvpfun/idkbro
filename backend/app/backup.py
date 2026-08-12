"""Memory backup — so the agent's brain is never lost.

Everything the agent learns lives in one SQLite file (data/idkbro.db). If the Codespace or
server is wiped, that file goes with it. This module makes timestamped snapshots on a timer
using SQLite's online backup API (safe to run while the DB is in use), and keeps the most
recent N. Optionally it can also commit snapshots to a git branch for off-machine safety.

Snapshots live in data/backups/. Restoring is just copying one back to data/idkbro.db.
"""

from __future__ import annotations

import asyncio
import glob
import os
import sqlite3
import time

from .config import settings

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_BACKUP_DIR = os.path.join(_DATA_DIR, "backups")
_DB_PATH = os.path.join(_DATA_DIR, "idkbro.db")


def make_snapshot() -> str | None:
    """Create one consistent snapshot of the live DB. Returns the path or None."""
    if not os.path.exists(_DB_PATH):
        return None
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(_BACKUP_DIR, f"idkbro-{stamp}.db")
    try:
        src = sqlite3.connect(_DB_PATH)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)  # online backup, safe while WAL is active
        dst.close()
        src.close()
        return dest
    except sqlite3.Error:
        return None


def _prune(keep: int) -> None:
    files = sorted(glob.glob(os.path.join(_BACKUP_DIR, "idkbro-*.db")))
    for old in files[:-keep] if keep > 0 else []:
        try:
            os.remove(old)
        except OSError:
            pass


async def backup_loop() -> None:
    """Periodic snapshots on a timer, pruned to the most recent N."""
    if not settings.backup_enabled:
        return
    await asyncio.sleep(60)  # first snapshot a minute after boot
    while True:
        try:
            path = make_snapshot()
            _prune(settings.backup_keep)
            if path and settings.backup_git_push:
                await _git_push(path)
        except Exception:
            pass
        await asyncio.sleep(max(300, settings.backup_interval_seconds))


async def _run(cmd: str, cwd: str, env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out.decode() if out else "")


async def _git_push(path: str) -> None:
    """Push the latest snapshot to a dedicated orphan branch as a SINGLE commit + file, so
    the brain survives even if the machine dies, without bloating the repo. Uses git plumbing
    (hash-object / update-index) against a temp index so it never touches the working tree or
    the code branch."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    branch = settings.backup_git_branch
    env = dict(os.environ)
    tmp_index = os.path.join(repo_root, ".git", "backup_index")
    env["GIT_INDEX_FILE"] = tmp_index

    try:
        # Blob for the snapshot.
        rc, blob = await _run(f"git hash-object -w '{path}'", repo_root)
        blob = blob.strip()
        if rc != 0 or not blob:
            return
        # Fresh temp index with just this one file named 'idkbro.db'.
        if os.path.exists(tmp_index):
            os.remove(tmp_index)
        rc, _ = await _run(
            f"git update-index --add --cacheinfo 100644,{blob},idkbro.db", repo_root, env
        )
        if rc != 0:
            return
        rc, tree = await _run("git write-tree", repo_root, env)
        tree = tree.strip()
        if rc != 0 or not tree:
            return
        # Single commit with no parent (orphan) => branch stays tiny.
        rc, commit = await _run(
            f'git commit-tree {tree} -m "memory {os.path.basename(path)}"', repo_root, env
        )
        commit = commit.strip()
        if rc != 0 or not commit:
            return
        await _run(f"git push origin {commit}:refs/heads/{branch} --force", repo_root)
    finally:
        if os.path.exists(tmp_index):
            try:
                os.remove(tmp_index)
            except OSError:
                pass
