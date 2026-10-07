"""Tests for Git-backed watchlist synchronization."""

from __future__ import annotations

import subprocess
from pathlib import Path

from shopper.config import GitHubSyncConfig
from shopper.github_sync import WatchlistGitSync


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _configure_git(path: Path) -> None:
    _git(path, "config", "user.name", "Shopper test")
    _git(path, "config", "user.email", "shopper@example.com")


def _write_watchlist(path: Path, name: str) -> None:
    (path / "watchlist.yaml").write_text(
        f"items:\n  - name: {name}\n    queries: [{name.lower()}]\n",
        encoding="utf-8",
    )


async def test_sync_keeps_local_watchlist_on_conflict_and_merges_remote_files(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
    _configure_git(seed)
    _write_watchlist(seed, "Base")
    _git(seed, "add", "watchlist.yaml")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "branch", "-M", "master")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "master")

    local = tmp_path / "local"
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    _configure_git(local)
    _configure_git(other)

    _write_watchlist(local, "Local")
    _write_watchlist(other, "Remote")
    (other / "remote-note.txt").write_text("from remote\n", encoding="utf-8")
    _git(other, "add", "watchlist.yaml", "remote-note.txt")
    _git(other, "commit", "-m", "remote watchlist update")
    _git(other, "push")

    sync = WatchlistGitSync(
        local / "watchlist.yaml",
        GitHubSyncConfig(enabled=True, interval_minutes=1),
    )
    # The local version wins the conflict, so no live watchlist reload is needed.
    assert not await sync.sync()

    assert "name: Local" in (local / "watchlist.yaml").read_text(encoding="utf-8")
    assert (local / "remote-note.txt").read_text(encoding="utf-8") == "from remote\n"
    assert "name: Local" in _git(local, "show", "origin/master:watchlist.yaml")
