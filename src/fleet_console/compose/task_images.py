"""Образы Harbor-задач и базовые образы сборки verifier при импорте."""

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


@dataclass(frozen=True)
class Manifest:
    url: str
    digest: str


def manifest_target(image: str) -> tuple[str, str, str] | ImageIssue:
    host, _, path = image.partition("/")
    if host.lower() not in _HOSTS:
        return ImageIssue(f"Укажите образ из внутреннего реестра {REGISTRY}", blocking=True)
    ref = _REF.fullmatch(path)
    if not ref:
        return ImageIssue("Некорректное имя, тег или digest образа", blocking=True)
    manifest = ref["digest"] or ref["tag"] or "latest"
    url = f"https://{host}/v2/{ref['repo']}/manifests/{manifest}"
    return host, ref["repo"], url


def _probe(client: httpx.Client, image: str) -> ImageIssue | Manifest:
    target = manifest_target(image)
    if isinstance(target, ImageIssue):
        return target
    host, repo, url = target
    headers = {"Accept": _ACCEPT}
    try:
        response = client.head(url, headers=headers)
        if response.status_code == 401 and response.headers.get("www-authenticate", "").lower().startswith("bearer "):
            # Anonymous pull on the internal Harbor. Never follow a server-provided token URL.
            auth = client.get(f"https://{host}/service/token", params={
                "service": "harbor-registry", "scope": f"repository:{repo}:pull",
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
        return Manifest(url, response.headers.get("Docker-Content-Digest", "").strip())
    except httpx.HTTPStatusError as exc:
        return ImageIssue(f"Не удалось проверить образ (HTTP {exc.response.status_code})", url)
    except (httpx.RequestError, KeyError, ValueError, TypeError):
        return ImageIssue("Не удалось проверить образ. Проверьте доступ к реестру и повторите проверку", url)


def check_images(images: dict[str, list[str]]) -> tuple[dict[str, ImageIssue], dict[str, str]]:
    issues: dict[str, ImageIssue] = {}
    digests: dict[str, str] = {}
    with httpx.Client(timeout=5, follow_redirects=False) as client, ThreadPoolExecutor(max_workers=6) as workers:
        results = workers.map(lambda image: _probe(client, image), images)
        for image, result in zip(images, results):
            messages = []
            blocking = False
            if isinstance(result, ImageIssue):
                messages.append(result.message)
                blocking = result.blocking
            elif result.digest:
                digests[image] = result.digest
            else:
                messages.append("Реестр не вернул Docker-Content-Digest.")
            if result.url and "@" not in image:
                messages.append("Тег может измениться. Чтобы закрепить образ для запуска, укажите ссылку с @sha256:…")
            if messages:
                issues[image] = ImageIssue(" ".join(messages), result.url, blocking)
    return issues, digests
