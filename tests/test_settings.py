from pathlib import Path
import shlex

from fleet_console import settings as config
from fleet_console.settings import Paths, Settings, default_home


def test_load_creates_defaults(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    settings = Settings.load(paths)
    assert settings.fleet_remote.endswith("fleet.git")
    assert settings.allowed_contexts == ["internal"]
    assert settings.bastion.ssh_host == "gigacode-bastion"
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


def test_previous_mlsc_address_is_migrated_without_rewriting_user_file(tmp_path):
    paths = Paths(tmp_path).ensure()
    original = "cluster:\n  mlsc_base_url: https://text-coordinator-proxy.mlrnd.ru/mlc/\n"
    paths.settings_file.write_text(original)
    assert Settings.load(paths).cluster.mlsc_base_url == "https://text-coordinator-proxy.sberai-rnd.ru/mlc/"
    assert paths.settings_file.read_text() == original
    paths.settings_file.write_text("cluster:\n  mlsc_base_url: https://custom/mlc/\n")
    assert Settings.load(paths).cluster.mlsc_base_url == "https://custom/mlc/"


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


def test_project_files_take_precedence_and_missing_files_fall_back(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "project with spaces"
    folder = repo / "config"
    folder.mkdir(parents=True)
    home = tmp_path / "home"
    legacy = (home / ".fleet-console")
    legacy.mkdir(parents=True)
    monkeypatch.setattr(config, "REPO_ROOT", repo)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("FLEET_CONSOLE_HOME", raising=False)
    for name in (*config.SECRET_NAMES, "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    (legacy / "settings.yaml").write_text("ui_port: 9091\n")
    (legacy / "secrets.env").write_text("MLSC_TOKEN=legacy\n")
    shared_s3 = home / ".aws" / "credentials"
    shared_s3.parent.mkdir()
    shared_s3.write_text("[gcvmfleet]\naws_access_key_id = legacy\n")

    paths = Paths.for_project()
    assert paths.home == legacy and Settings.load(paths).ui_port == 9091
    assert paths.ssh_command() == ["ssh"] and paths.subprocess_env() == {}
    assert config.read_secret(paths, "MLSC_TOKEN") == "legacy"
    assert config.read_secret(paths, "AWS_ACCESS_KEY_ID_gcvmfleet") == "legacy"

    (folder / "settings.yaml").write_text("ui_port: 18082\ndatasets_root: ../data\n")
    (folder / "secrets.env").write_text("MLSC_TOKEN=project\n")
    (folder / "aws-credentials").write_text("[gcvmfleet]\naws_access_key_id = project\n")
    (folder / "kubeconfig").touch()
    (folder / "ssh_config").touch()
    paths = Paths.for_project()
    settings = Settings.load(paths)
    assert paths.home == repo / ".fleet-console"
    assert settings.ui_port == 18082 and settings.datasets_root_path() == repo / "data"
    settings.save(paths)
    assert (legacy / "settings.yaml").read_text() == "ui_port: 9091\n"
    assert config.read_secret(paths, "MLSC_TOKEN") == "project"
    assert config.read_secret(paths, "AWS_ACCESS_KEY_ID_gcvmfleet") == "project"
    assert paths.subprocess_env() == {
        "KUBECONFIG": str(folder / "kubeconfig"),
        "AWS_SHARED_CREDENTIALS_FILE": str(folder / "aws-credentials"),
        "GIT_SSH_COMMAND": shlex.join(["ssh", "-F", str(folder / "ssh_config")]),
    }
    (folder / "aws-credentials").unlink()
    (folder / "secrets.env").unlink()
    assert config.read_secret(paths, "MLSC_TOKEN") == "legacy"
    assert config.read_secret(paths, "AWS_ACCESS_KEY_ID_gcvmfleet") == "legacy"
