"""Признаки одной Harbor-задачи для проверки папки и preflight выбранного JobConfig."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskFeatures:
    network_mode: str
    allowed_hosts: tuple[str, ...]
    has_mcp: bool
    verifier_mode: str
    verifier_image: str | None

    @classmethod
    def from_config(cls, config: dict) -> "TaskFeatures":
        env = config.get("environment", {})
        verifier = config.get("verifier", {})
        if not isinstance(env, dict) or not isinstance(verifier, dict):
            raise TypeError("environment и verifier должны быть таблицами TOML")
        separate_env = verifier.get("environment")
        if separate_env is not None and not isinstance(separate_env, dict):
            raise TypeError("verifier.environment должен быть таблицей TOML")
        mode = verifier.get("environment_mode", "separate" if separate_env is not None else "shared")
        image = (env if separate_env is None else separate_env).get("docker_image")
        network = env.get("network_mode") or ("no_network" if env.get("allow_internet") is False else "public")
        return cls(network, tuple(env.get("allowed_hosts") or ()), bool(env.get("mcp_servers")), mode,
                   image if mode == "separate" else None)
