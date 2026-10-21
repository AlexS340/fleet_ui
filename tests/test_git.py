from pathlib import Path

import pytest

from fleet_console.fleetref.git import Mirror
from fleet_console.runner import CommandError, SubprocessRunner
from fleet_console.settings import Paths
from conftest import commit_file


@pytest.fixture
async def mirror(remote_repo: Path, paths: Paths) -> Mirror:
    m = Mirror(SubprocessRunner(), paths, str(remote_repo))
    assert await m.ensure() is True
    assert await m.ensure() is False
    return m


async def test_branches_sorted_by_date_with_author(mirror: Mirror) -> None:
    branches = await mirror.branches()
    assert [b.name for b in branches] == ["master", "feat/x"]
    assert branches[1].author == "Dasha"
    assert branches[0].date.year == 2026 and branches[0].date.tzinfo is not None
    assert mirror.last_fetch_at() is not None


async def test_commits_resolve_and_info(mirror: Mirror) -> None:
    commits = await mirror.commits("master", limit=50)
    assert [c.subject for c in commits] == ["master moves on", "init"]
    feat_sha = await mirror.resolve("feat/x")
    assert feat_sha == (await mirror.commits("feat/x", 1))[0].sha
    info = await mirror.commit_info(feat_sha)
    assert info.merged_into_main is False and info.behind_main == 1 and info.author == "Dasha"
    master = await mirror.commit_info(await mirror.resolve("master"))
    assert master.merged_into_main is True and master.behind_main == 0
    with pytest.raises(CommandError):
        await mirror.resolve("no-such-ref")


async def test_fetch_sees_new_commit(mirror: Mirror, remote_repo: Path) -> None:
    commit_file(remote_repo, "new.txt", "n\n", "newer")
    assert (await mirror.commits("master", 1))[0].subject == "master moves on"
    await mirror.fetch()
    assert (await mirror.commits("master", 1))[0].subject == "newer"


async def test_worktree_lifecycle(mirror: Mirror) -> None:
    sha = await mirror.resolve("feat/x")
    path = await mirror.worktree_add(sha)
    assert path == mirror.worktree_path(sha) and (path / "feat.txt").exists()
    assert await mirror.worktree_add(sha) == path  # идемпотентно
    assert mirror.worktrees() == [sha]
    assert await mirror.is_clean(path)
    (path / ".venv" / "bin").mkdir(parents=True)
    (path / ".venv" / "bin" / "fleet").touch()
    assert await mirror.is_clean(path)  # .venv сервиса не делает worktree грязным
    (path / "feat.txt").write_text("dirty")
    assert not await mirror.is_clean(path)
    await mirror.worktree_remove(sha)
    assert not path.exists() and mirror.worktrees() == []
