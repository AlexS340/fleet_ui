from pathlib import Path

import pytest

from fleet_console.fleetref import profile as prof
from fleet_console.fleetref.sync import fleet_argv, proxy_env, uv_sync, venv_ready
from fleet_console.runner import FakeRunner, Result

HELP = """Usage: fleet [OPTIONS] [CONFIG_PATH]

Options:
  -w, --watch-directory DIRECTORY
                                  Отслеживать каталог
  -o, --overrides TEXT            Hydra-style overrides
  --tui / --no-tui                TUI
  --argo-host TEXT                URL Argo Server API
  --print-config                  Напечатать composed Hydra-конфиг
  --help                          Show this message and exit.
"""
HELP_OLD = HELP.replace("  --print-config                  Напечатать composed Hydra-конфиг\n", "")
RESOLVED = """
system:
  agent_image: registry/gc-cli-harbor:v0.3.0
  materialize:
    _target_: fleet.HarborTaskMaterializer
  polar:
    topology: {}
mlsc:
  create_job:
    _target_: fleet.create_job.CreateJob
  stop_llm:
    _target_: fleet.mlsc.SetInstances
  version_id_file: x
harbor_job:
  _target_: harbor.JobConfig
main:
  run: [mlsc.create_job]
"""


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    cfg = wt / prof.CONFIGS_REL
    for rel, text in {
        "pyproject.toml": 'dependencies = [\n    "harbor==0.22.0",\n]\n',
        f"{prof.CONFIGS_REL}/experiment_template.yaml": "defaults: []\n",
        f"{prof.CONFIGS_REL}/mlsc/model/glm.yaml": "x: 1\n",
        f"{prof.CONFIGS_REL}/mlsc/model/qwen.yaml": "x: 1\n",
        f"{prof.CONFIGS_REL}/mlsc/common.yaml": "x: 1\n",
        f"{prof.CONFIGS_REL}/harbor/agent/terminus2.yaml": "x: 1\n",
        f"{prof.CONFIGS_REL}/harbor/dataset/bird.yaml": "x: 1\n",
        f"{prof.CONFIGS_REL}/mode/run.yaml": "x: 1\n",
        f"{prof.CONFIGS_REL}/datasets/z.yaml": "x: 1\n",
    }.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text, encoding="utf-8")
    assert cfg.is_dir()
    return wt


def test_argv_env_and_venv(tmp_path: Path) -> None:
    assert fleet_argv(tmp_path, "--help") == ["uv", "run", "--project", str(tmp_path), "--no-sync", "fleet", "--help"]
    assert proxy_env(None) == {} and proxy_env("socks5h://127.0.0.1:1080")["HTTPS_PROXY"] == "socks5h://127.0.0.1:1080"
    assert not venv_ready(tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "fleet").touch()
    assert venv_ready(tmp_path)


async def test_uv_sync_uses_proxy_and_log(tmp_path: Path) -> None:
    fake = FakeRunner()
    fake.on("uv", "sync", "--frozen", stdout="Resolved 1 package\nInstalled")
    lines: list[str] = []
    result = await uv_sync(fake, tmp_path, log_path=tmp_path / "sync.log", proxy="socks5h://p:1", on_line=lines.append)
    assert result.ok and lines == ["Resolved 1 package", "Installed"]
    argv, meta = fake.calls[0]
    assert meta["cwd"] == tmp_path and meta["env"]["HTTPS_PROXY"] == "socks5h://p:1"
    assert (tmp_path / "sync.log").exists()


def test_parse_help_and_steps() -> None:
    flags = prof.parse_help(HELP)
    assert "--print-config" in flags and "-w" in flags and "--no-tui" in flags and "--help" in flags
    assert "--print-config" not in prof.parse_help(HELP_OLD)
    import yaml

    assert prof.step_sections(yaml.safe_load(RESOLVED)) == ["system.materialize", "mlsc.create_job", "mlsc.stop_llm"]


async def test_build_full_profile(worktree: Path) -> None:
    fake = FakeRunner()
    fake.on("uv", "run", "--project", str(worktree), "--no-sync", "fleet", "--help", stdout=HELP)
    fake.on("uv", "run", "--project", str(worktree), "--no-sync", "fleet", "examples/harbor_new/configs/experiment_template.yaml", stdout=RESOLVED)
    p = await prof.build(fake, worktree, "abc")
    assert p.layout == "harbor_new" and p.compatible and p.missing == []
    assert p.groups["mlsc/model"] == ["glm", "qwen"] and p.groups["mlsc"] == ["common"] and "datasets" in p.groups
    assert p.harbor_version == "0.22.0" and p.agent_image == "registry/gc-cli-harbor:v0.3.0"
    assert p.steps == ["system.materialize", "mlsc.create_job", "mlsc.stop_llm"] and p.supports_step("mlsc.stop_llm")
    argv, meta = fake.calls[1]
    assert "-o" in argv and "mlsc/model=glm" in argv and "experiment.jobs=1" in argv
    assert meta["env"]["MLSC_TOKEN"] == "fleet-console-dummy"
    assert prof.Profile.from_dict(p.to_dict()) == p


async def test_build_legacy_layout_skips_print_config(tmp_path: Path) -> None:
    wt = tmp_path / "old"
    wt.mkdir()
    (wt / "pyproject.toml").write_text('"harbor==0.21.0"')
    fake = FakeRunner()
    fake.on("uv", stdout=HELP)
    p = await prof.build(fake, wt, "old")
    assert p.layout == "legacy" and not p.compatible and p.harbor_version == "0.21.0"
    assert any("старая раскладка" in m for m in p.missing)
    assert len(fake.calls) == 1  # только --help


async def test_build_without_print_config_flag(worktree: Path) -> None:
    fake = FakeRunner()
    fake.on("uv", stdout=HELP_OLD)
    p = await prof.build(fake, worktree, "x")
    assert not p.print_config and p.steps == [] and any("--print-config" in m for m in p.missing)


async def test_build_print_config_failure(worktree: Path) -> None:
    fake = FakeRunner()
    fake.on("uv", "run", "--project", str(worktree), "--no-sync", "fleet", "--help", stdout=HELP)
    fake.on("uv", result=lambda argv: Result(argv, 1, "", "Hydra error\nMissing mandatory value: mlsc/model"))
    p = await prof.build(fake, worktree, "x")
    assert p.print_config and p.steps == [] and "Missing mandatory value" in p.missing[0]
    assert not p.compatible
