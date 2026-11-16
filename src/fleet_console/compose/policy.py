"""Локальные проверки заявки: ресурсы и приватный маршрут результатов, без внешних действий."""

import ast
from pathlib import Path
import re
from urllib.parse import urlparse

from fleet_console.settings import Settings


def private_root(settings: Settings) -> str:
    root = settings.results.private_s3_root.strip().rstrip("/")
    if not root:
        if settings.auth.mode == "local" or settings.results.allow_shared_bucket:
            return ""
        raise ValueError("Настройте закрытый S3-корень результатов (results.private_s3_root) "
                         "или разрешите общий бакет Fleet (results.allow_shared_bucket)")
    parsed = urlparse(root)
    if (parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/")
            or parsed.query or parsed.fragment or not re.fullmatch(r"s3://[a-z0-9.-]+/[A-Za-z0-9/_-]+", root)
            or any(p in {"", ".", ".."} for p in parsed.path.strip("/").split("/"))):
        raise ValueError("Настройте закрытый S3-корень результатов (results.private_s3_root: s3://bucket/prefix)")
    return root


def supports_private_measure(worktree: Path) -> bool:
    """Проверяем контракт закреплённого SHA без импорта fleet и без запуска кода."""
    try:
        tree = ast.parse((worktree / "fleet" / "measure_run.py").read_text())
    except (OSError, SyntaxError):
        return False
    return any(isinstance(n, ast.ClassDef) and n.name == "MeasureRunRunner"
               and any(isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
                       and field.target.id == "read_only" for field in n.body) for n in tree.body)


def check_gpu(gpu: int | None, settings: Settings, *, user_limit: int | None = None) -> int:
    if type(gpu) is not int or gpu < 0:
        raise ValueError("Не удалось определить число GPU из итогового конфига; заявка не принята")
    if gpu > min(user_limit if user_limit is not None else settings.quotas.user_gpus, settings.quotas.total_gpus):
        raise ValueError(f"Заявка требует {gpu} GPU и превышает квоту; уменьшите число реплик")
    return gpu
