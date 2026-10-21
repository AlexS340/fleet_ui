"""Зеркало репозитория fleet и worktree на точный SHA. Только git CLI через Runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil

from fleet_console.runner import CommandError, LineHook, Result, Runner
from fleet_console.settings import Paths

MAIN_BRANCH = "master"
SEP = "\x09"


@dataclass(frozen=True)
class Branch:
    name: str
    sha: str
    date: datetime
    author: str


@dataclass(frozen=True)
class Commit:
    sha: str
    date: datetime
    author: str
    subject: str

    @property
    def short(self) -> str:
        return self.sha[:8]


@dataclass(frozen=True)
class CommitInfo(Commit):
    merged_into_main: bool
    behind_main: int


def _check(result: Result) -> Result:
    if not result.ok:
        raise CommandError(result)
    return result


def _parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.strip())


class Mirror:
    """`git clone --mirror` fleet в paths.mirror и worktree в paths.worktrees/<sha>."""

    def __init__(self, runner: Runner, paths: Paths, remote: str) -> None:
        self.runner = runner
        self.paths = paths
        self.remote = remote

    @property
    def path(self) -> Path:
        return self.paths.mirror

    def exists(self) -> bool:
        return (self.path / "HEAD").exists()

    async def _git(self, *args: str) -> Result:
        return _check(await self.runner.run(["git", f"--git-dir={self.path}", *args]))

    # --- зеркало -----------------------------------------------------------
    async def ensure(self, on_line: LineHook | None = None) -> bool:
        """Склонировать зеркало, если его ещё нет. Возвращает True, если клонировали."""
        if self.exists():
            return False
        self.paths.ensure()
        log = self.paths.logs / "clone.log"
        result = await self.runner.stream(
            ["git", "clone", "--mirror", "--progress", self.remote, str(self.path)], log_path=log, on_line=on_line
        )
        _check(result)
        return True

    async def fetch(self) -> None:
        await self._git("fetch", "--prune", "--quiet")
        (self.path / "fleet-console.last_fetch").touch()

    def last_fetch_at(self) -> datetime | None:
        marker = self.path / "fleet-console.last_fetch"
        probe = marker if marker.exists() else (self.path / "HEAD" if self.exists() else None)
        if probe is None:
            return None
        return datetime.fromtimestamp(probe.stat().st_mtime, tz=timezone.utc)

    # --- ветки и коммиты -----------------------------------------------------
    async def branches(self) -> list[Branch]:
        fmt = SEP.join(["%(refname:short)", "%(objectname)", "%(committerdate:iso8601-strict)", "%(authorname)"])
        result = await self._git("for-each-ref", "--sort=-committerdate", f"--format={fmt}", "refs/heads/")
        branches = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            name, sha, date, author = line.split(SEP, 3)
            branches.append(Branch(name, sha, _parse_date(date), author))
        return branches

    async def commits(self, ref: str, limit: int = 50) -> list[Commit]:
        fmt = SEP.join(["%H", "%cI", "%an", "%s"])
        result = await self._git("log", f"-n{limit}", f"--format={fmt}", ref, "--")
        commits = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            sha, date, author, subject = line.split(SEP, 3)
            commits.append(Commit(sha, _parse_date(date), author, subject))
        return commits

    async def resolve(self, ref: str) -> str:
        """Ветка, тег или SHA → полный SHA коммита."""
        result = await self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return result.stdout.strip()

    async def commit_info(self, sha: str, main: str = MAIN_BRANCH) -> CommitInfo:
        (head,) = await self.commits(sha, limit=1)
        merged = await self.runner.run(["git", f"--git-dir={self.path}", "merge-base", "--is-ancestor", sha, main])
        behind = await self._git("rev-list", "--count", f"{sha}..{main}")
        return CommitInfo(
            sha=head.sha,
            date=head.date,
            author=head.author,
            subject=head.subject,
            merged_into_main=merged.ok,
            behind_main=int(behind.stdout.strip() or 0),
        )

    # --- worktree ------------------------------------------------------------
    def worktree_path(self, sha: str) -> Path:
        return self.paths.worktrees / sha

    def worktrees(self) -> list[str]:
        if not self.paths.worktrees.exists():
            return []
        return sorted(p.name for p in self.paths.worktrees.iterdir() if (p / ".git").exists())

    async def worktree_add(self, sha: str) -> Path:
        """Создать detached worktree на SHA; существующий — вернуть как есть."""
        path = self.worktree_path(sha)
        if (path / ".git").exists():
            return path
        self.paths.ensure()
        if path.exists():
            shutil.rmtree(path)
        await self._git("worktree", "prune")
        await self._git("worktree", "add", "--detach", str(path), sha)
        return path

    async def worktree_remove(self, sha: str) -> None:
        path = self.worktree_path(sha)
        if path.exists():
            await self._git("worktree", "remove", "--force", str(path))
        await self._git("worktree", "prune")

    async def is_clean(self, path: Path) -> bool:
        """Нет изменений и чужих файлов; `.venv` — артефакт сервиса, не считается."""
        argv = ["git", "-C", str(path), "status", "--porcelain", "--", ".", ":(exclude).venv"]
        result = _check(await self.runner.run(argv))
        return result.stdout.strip() == ""
