from pathlib import Path

from fleet_console.settings import Paths, Settings, default_home


def test_load_creates_defaults(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    settings = Settings.load(paths)
    assert settings.fleet_remote.endswith("fleet.git")
    assert settings.allowed_contexts == ["internal"]
    assert settings.bastion.ssh_host == "fleetbastion"
    assert paths.settings_file.exists()
    assert paths.worktrees.is_dir() and paths.logs.is_dir()
    assert paths.settings_file.read_text(encoding="utf-8").startswith("# Настройки Fleet Console")


def test_round_trip_and_partial_file(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    paths.ensure()
    paths.settings_file.write_text("namespace: other\nbastion:\n  ssh_host: ''\n", encoding="utf-8")
    settings = Settings.load(paths)
    assert settings.namespace == "other"
    assert settings.bastion.ssh_host == ""
    assert settings.bastion.socks_proxy.startswith("socks5h://")  # дефолт сохранился
    settings.auto_fetch_minutes = 15
    settings.save(paths)
    assert Settings.load(paths).auto_fetch_minutes == 15


def test_home_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FLEET_CONSOLE_HOME", str(tmp_path / "x"))
    assert default_home() == tmp_path / "x"
    assert Paths(default_home()).db == tmp_path / "x" / "console.sqlite"
