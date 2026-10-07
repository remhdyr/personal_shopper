"""Synchronize the managed watchlist with its GitHub-backed Git repository."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import subprocess
from pathlib import Path

from .config import GitHubSyncConfig
from .logging_setup import get_logger

log = get_logger(__name__)


class WatchlistGitSync:
    """Fetch, merge, and push a watchlist without touching unrelated edits.

    Dashboard edits are committed as a single-file commit before fetching. On a
    true merge conflict, local dashboard edits win only for ``watchlist.yaml``;
    conflicts in any other file abort the merge rather than silently choosing a
    version of application code.
    """

    def __init__(self, watchlist_path: str | Path, config: GitHubSyncConfig) -> None:
        self._watchlist_path = Path(watchlist_path)
        self._config = config
        self._unavailable_logged = False

    async def sync(self) -> bool:
        """Synchronize and return whether the on-disk watchlist changed."""

        return await asyncio.to_thread(self._sync)

    def _sync(self) -> bool:
        # Share the updater's lock: it tests and fast-forwards this same
        # worktree, so Git operations must never overlap.
        with Path("/var/lib/shopper/shopper-update.lock").open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.info("GitHub watchlist sync skipped: Shopper update is running")
                return False
            return self._sync_locked()

    def _sync_locked(self) -> bool:
        if not self._watchlist_path.exists():
            log.warning("GitHub watchlist sync skipped: %s does not exist", self._watchlist_path)
            return False

        repo = self._repository_root()
        if repo is None:
            return False
        watchlist = self._relative_path(repo)
        if watchlist is None or not self._is_tracked(repo, watchlist):
            return False
        if self._has_unrelated_staged_changes(repo, watchlist):
            log.warning("GitHub watchlist sync skipped: unrelated staged changes are present")
            return False

        before = self._digest()
        self._commit_watchlist(repo, watchlist)
        upstream = self._upstream(repo)
        if upstream is None:
            return False
        self._run(repo, "fetch", self._config.remote)
        if not self._merge(repo, upstream, watchlist):
            return False
        self._run(repo, "push", self._config.remote, f"HEAD:{upstream.split('/', 1)[1]}")
        changed = self._digest() != before
        if changed:
            log.info("Synchronized watchlist from GitHub")
        return changed

    def _repository_root(self) -> Path | None:
        try:
            return Path(self._run(self._watchlist_path.parent, "rev-parse", "--show-toplevel"))
        except subprocess.CalledProcessError:
            if not self._unavailable_logged:
                log.warning(
                    "GitHub watchlist sync disabled: %s is not in a Git worktree",
                    self._watchlist_path,
                )
                self._unavailable_logged = True
            return None

    def _relative_path(self, repo: Path) -> str | None:
        try:
            return str(self._watchlist_path.resolve().relative_to(repo.resolve()))
        except ValueError:
            log.warning(
                "GitHub watchlist sync skipped: %s is outside repository %s",
                self._watchlist_path,
                repo,
            )
            return None

    def _is_tracked(self, repo: Path, watchlist: str) -> bool:
        try:
            self._run(repo, "ls-files", "--error-unmatch", "--", watchlist)
        except subprocess.CalledProcessError:
            log.warning("GitHub watchlist sync skipped: %s is not tracked", watchlist)
            return False
        return True

    def _upstream(self, repo: Path) -> str | None:
        try:
            upstream = self._run(
                repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
            )
        except subprocess.CalledProcessError:
            log.warning("GitHub watchlist sync skipped: current branch has no upstream")
            return None
        remote, _, branch = upstream.partition("/")
        if remote != self._config.remote or not branch:
            log.warning(
                "GitHub watchlist sync skipped: upstream %s does not use remote %s",
                upstream,
                self._config.remote,
            )
            return None
        return upstream

    def _commit_watchlist(self, repo: Path, watchlist: str) -> None:
        self._run(repo, "add", "--", watchlist)
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", watchlist],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        if result.returncode != 1:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )
        self._run(
            repo,
            "commit",
            "--only",
            "-m",
            "chore(watchlist): sync dashboard changes",
            "--",
            watchlist,
        )

    def _merge(self, repo: Path, upstream: str, watchlist: str) -> bool:
        already_merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", upstream, "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if already_merged.returncode == 0:
            return True
        if already_merged.returncode != 1:
            raise subprocess.CalledProcessError(
                already_merged.returncode,
                already_merged.args,
                already_merged.stdout,
                already_merged.stderr,
            )
        result = subprocess.run(
            ["git", "-C", str(repo), "merge", "--no-ff", "--no-commit", upstream],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            if self._has_staged_changes(repo):
                self._run(repo, "commit", "--no-edit")
            return True

        conflicts = self._run(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
        if conflicts == [watchlist]:
            log.warning("Watchlist merge conflict; retaining local dashboard edits")
            self._run(repo, "checkout", "--ours", "--", watchlist)
            self._run(repo, "add", "--", watchlist)
            self._run(repo, "commit", "--no-edit")
            return True

        self._run(repo, "merge", "--abort")
        log.error(
            "GitHub sync skipped: merge conflicts outside %s require manual resolution: %s",
            watchlist,
            ", ".join(conflicts) or result.stderr.strip(),
        )
        return False

    @staticmethod
    def _has_unrelated_staged_changes(repo: Path, watchlist: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        )
        return any(path != watchlist for path in result.stdout.splitlines())

    def _has_staged_changes(self, repo: Path) -> bool:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode in (0, 1):
            return result.returncode == 1
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )

    def _digest(self) -> str:
        return hashlib.sha256(self._watchlist_path.read_bytes()).hexdigest()

    @staticmethod
    def _run(repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
