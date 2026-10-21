"""Общие фикстуры: временный «удалённый» репозиторий fleet без сети."""

pytest_plugins = ["nicegui.testing.user_plugin"]

from pathlib import Path
import subprocess

import pytest

from fleet_console.settings import Paths

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Egor",
    "GIT_AUTHOR_EMAIL": "egor@example.com",
    "GIT_COMMITTER_NAME": "Egor",
    "GIT_COMMITTER_EMAIL": "egor@example.com",
}


def git(repo: Path, *args: str, date: str | None = None, author: str | None = None) -> str:
    env = {**GIT_ENV}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    if author:
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = author
    out = subprocess.run(
        ["git", "-c", "init.defaultBranch=master", *args], cwd=repo, env={**env, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def commit_file(repo: Path, rel: str, text: str, message: str, **kw) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", message, **kw)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    """master с двумя коммитами и ветка feat/x, отстающая от master на один коммит."""
    repo = tmp_path / "remote"
    repo.mkdir()
    git(repo, "init", "-q")
    commit_file(repo, "pyproject.toml", 'name = "fleet"\n', "init", date="2026-10-01T10:00:00+03:00")
    git(repo, "checkout", "-q", "-b", "feat/x")
    commit_file(repo, "feat.txt", "x\n", "feature work", date="2026-10-02T10:00:00+03:00", author="Dasha")
    git(repo, "checkout", "-q", "master")
    commit_file(repo, "README.md", "fleet\n", "master moves on", date="2026-10-03T10:00:00+03:00")
    return repo


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "home").ensure()
