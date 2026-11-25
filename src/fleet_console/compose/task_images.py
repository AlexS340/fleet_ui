"""Проверка манифестов основных образов Harbor-задач при импорте."""

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx

REGISTRY = "registry.mlrnd.ru"
_HOSTS = {REGISTRY, f"{REGISTRY}:443", f"{REGISTRY}:1443"}
_PART = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REF = re.compile(
    rf"(?P<repo>{_PART}(?:/{_PART})*)(?::(?P<tag>[\w][\w.-]{{0,127}}))?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}|sha384:[0-9a-f]{96}|sha512:[0-9a-f]{128}))?", re.ASCII,
)
_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json"
)


@dataclass(frozen=True)
class ImageIssue:
    message: str
    url: str = ""
    blocking: bool = False


def _probe(client: httpx.Client, image: str) -> ImageIssue | None:
    host, _, path = image.partition("/")
    if host.lower() not in _HOSTS:
        return ImageIssue(f"Проверка доступна для образов из {REGISTRY}")
    ref = _REF.fullmatch(path)
    if not ref:
        return ImageIssue("Некорректное имя, тег или digest образа", blocking=True)
    manifest = ref["digest"] or ref["tag"] or "latest"
    url = f"https://{host}/v2/{ref['repo']}/manifests/{manifest}"
    headers = {"Accept": _ACCEPT}
    try:
        response = client.head(url, headers=headers)
        if response.status_code == 401 and response.headers.get("www-authenticate", "").lower().startswith("bearer "):
            # Anonymous pull on the internal Harbor. Never follow a server-provided token URL.
            auth = client.get(f"https://{host}/service/token", params={
                "service": "harbor-registry", "scope": f"repository:{ref['repo']}:pull",
            })
            auth.raise_for_status()
            token = auth.json()["token"]
            if not isinstance(token, str) or not token or not token.isascii() or not token.isprintable():
                raise ValueError("invalid registry token")
            response = client.head(url, headers={**headers, "Authorization": f"Bearer {token}"})
        if response.status_code == 404:
            return ImageIssue("Образ отсутствует в реестре", url, blocking=True)
        response.raise_for_status()
        if response.status_code != 200:
            return ImageIssue(f"Не удалось проверить образ (HTTP {response.status_code})", url)
        return None
    except httpx.HTTPStatusError as exc:
        return ImageIssue(f"Не удалось проверить образ (HTTP {exc.response.status_code})", url)
    except (httpx.RequestError, KeyError, ValueError, TypeError):
        return ImageIssue("Не удалось проверить образ. Проверьте доступ к реестру и повторите проверку", url)


def check_images(images: dict[str, list[str]]) -> dict[str, ImageIssue]:
    with httpx.Client(timeout=5, follow_redirects=False) as client, ThreadPoolExecutor(max_workers=6) as workers:
        results = workers.map(lambda image: _probe(client, image), images)
        return {image: issue for image, issue in zip(images, results) if issue is not None}
