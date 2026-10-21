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


def test_aws_keys_come_from_credentials_profile(tmp_path, monkeypatch) -> None:
    from fleet_console.settings import Paths, read_secret, secrets_env

    paths = Paths(tmp_path / "home").ensure()
    creds = tmp_path / "credentials"
    creds.write_text("[default]\naws_access_key_id = other\n\n[gcvmfleet]\naws_access_key_id = AKIA\naws_secret_access_key = s3cr3t \n", encoding="utf-8")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID_gcvmfleet", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY_gcvmfleet", raising=False)
    assert read_secret(paths, "AWS_ACCESS_KEY_ID_gcvmfleet", creds) == "AKIA"
    assert read_secret(paths, "AWS_SECRET_ACCESS_KEY_gcvmfleet", creds) == "s3cr3t"
    assert read_secret(paths, "AWS_ACCESS_KEY_ID_other", creds) is None and read_secret(paths, "MLSC_TOKEN", creds) is None
    paths.secrets_env.write_text("AWS_ACCESS_KEY_ID_gcvmfleet=from-env-file\n", encoding="utf-8")
    assert read_secret(paths, "AWS_ACCESS_KEY_ID_gcvmfleet", creds) == "from-env-file"  # secrets.env важнее профиля
    monkeypatch.setenv("AWS_ACCESS_KEY_ID_gcvmfleet", "from-env")
    assert read_secret(paths, "AWS_ACCESS_KEY_ID_gcvmfleet", creds) == "from-env"  # окружение важнее всего
    assert read_secret(paths, "AWS_SECRET_ACCESS_KEY_gcvmfleet", tmp_path / "missing") is None
    assert set(secrets_env(paths, ("AWS_ACCESS_KEY_ID_gcvmfleet", "MLSC_TOKEN"))) == {"AWS_ACCESS_KEY_ID_gcvmfleet"}
