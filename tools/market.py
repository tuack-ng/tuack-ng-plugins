"""插件市场索引的加载 / 校验 / 生成。

元数据字段与 tuack-ng 的插件清单 `plugin.toml`（registry）对齐：

    name / version / description / authors / license / repo_url / url / pluginapi

市场特有：`artifact_name`（release 归档名）。下载地址由 `repo_url` + `version` +
`artifact_name` 推导为对应 release 的资产；`sha256` 取自该 release 资产的 `digest`。

用 pydantic 定义与校验模型；未知字段忽略，因此 `plugin.toml` 与市场元数据可互相兼容。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"
INDEX_FILE = ROOT / "index.json"

SCHEMA = 1
GITHUB = "https://github.com"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
REPO_URL_RE = re.compile(
    r"^https://github\.com/([0-9A-Za-z_.-]+)/([0-9A-Za-z_.-]+?)(?:\.git)?/?$"
)


class PluginMeta(BaseModel):
    """`plugins/<name>.toml`，字段与 `plugin.toml` 对齐。"""

    # 同一文件也可作为 plugin.toml（含 components 等），故忽略未知字段
    model_config = ConfigDict(extra="ignore")

    name: str
    version: str
    description: str
    authors: list[str] = Field(default_factory=list)
    license: str
    repo_url: str
    url: str | None = None
    pluginapi: str
    artifact_name: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError("只允许小写字母、数字与 . _ -")
        return value

    @field_validator("version", "pluginapi")
    @classmethod
    def _check_semver(cls, value: str) -> str:
        if not SEMVER_RE.match(value):
            raise ValueError("必须是语义化版本")
        return value

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, value: str) -> str:
        if not REPO_URL_RE.match(value):
            raise ValueError("必须是 https://github.com/<owner>/<name>")
        return value

    @field_validator("description", "license", "artifact_name")
    @classmethod
    def _check_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空")
        return value


def token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or None


def load_plugin(path: Path) -> PluginMeta:
    """读取并校验单个元数据文件（pydantic）。"""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return PluginMeta.model_validate(data)


def format_errors(exc: Exception) -> list[str]:
    """把 pydantic 校验错误转成可读行。"""
    if isinstance(exc, ValidationError):
        lines = []
        for error in exc.errors():
            loc = ".".join(str(part) for part in error["loc"]) or "(根)"
            lines.append(f"{loc}: {error['msg']}")
        return lines
    return [str(exc)]


def repo_of(repo_url: str) -> str:
    """从仓库链接解析出 `owner/name`。"""
    match = REPO_URL_RE.match(repo_url)
    if not match:
        raise ValueError(
            f"repo_url 必须是 https://github.com/<owner>/<name>：{repo_url!r}"
        )
    return f"{match.group(1)}/{match.group(2)}"


def normalize_version(value: str) -> str:
    return value.removeprefix("v")


def download_url(repo: str, tag: str, artifact_name: str) -> str:
    """发布归档下载地址：对应 release（tag）的资产。"""
    return f"{GITHUB}/{repo}/releases/download/{tag}/{artifact_name}"


def _request(url: str) -> urllib.request.Request:
    headers = {"User-Agent": "tuack-ng-plugins-ci"}
    if url.startswith("https://api.github.com") and (tok := token()):
        headers["Authorization"] = f"Bearer {tok}"
    return urllib.request.Request(url, headers=headers)


def fetch(url: str, timeout: int = 120) -> bytes:
    with urllib.request.urlopen(_request(url), timeout=timeout) as response:
        return response.read()


def release_by_tag(repo: str, version: str) -> dict:
    """按版本号查询对应的 release（依次尝试 tag `version` 与 `v{version}`）。"""
    last: Exception | None = None
    for tag in (version, f"v{version}"):
        try:
            return json.loads(
                fetch(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            last = exc
    raise ValueError(f"{repo}: 找不到版本 {version} 的 release") from last


def asset_digest(release: dict, artifact_name: str) -> str | None:
    """从 release 资产信息里取 sha256（GitHub `digest` 形如 `sha256:...`）。"""
    for item in release.get("assets", []):
        if item.get("name") == artifact_name:
            digest = item.get("digest") or ""
            return digest.removeprefix("sha256:") or None
    return None


def extract_plugin_toml(data: bytes, filename: str) -> bytes | None:
    """从发布归档里取出 `plugin.toml`（归档根或一层子目录内）。"""
    lower = filename.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = _find_manifest(zf.namelist())
            return zf.read(member) if member else None
    if lower.endswith((".tar.gz", ".tgz")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            member = _find_manifest(tf.getnames())
            if member is None:
                return None
            handle = tf.extractfile(member)
            return handle.read() if handle else None
    return None


def _find_manifest(names: list[str]) -> str | None:
    candidates = [
        n
        for n in names
        if not n.endswith("/") and n.rsplit("/", 1)[-1] == "plugin.toml"
    ]
    for name in candidates:
        if name.count("/") == 0:
            return name
    for name in candidates:
        if name.count("/") == 1:
            return name
    return candidates[0] if candidates else None


def verify_remote(meta: PluginMeta) -> list[str]:
    """联网校验：对应 release 版本/资产、资产 digest，并下载归档核对内层 plugin.toml。"""
    errors: list[str] = []
    repo = repo_of(meta.repo_url)
    version = normalize_version(meta.version)

    try:
        release = release_by_tag(repo, meta.version)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return [f"查询 {repo} 的 release 失败：{exc}"]
    except ValueError as exc:
        return [str(exc)]

    tag = str(release.get("tag_name", ""))
    if normalize_version(tag) != version:
        errors.append(f"release tag `{tag}` 与 version `{meta.version}` 不一致")

    digest = asset_digest(release, meta.artifact_name)
    if digest is None:
        errors.append(f"release `{tag}` 资产 `{meta.artifact_name}` 缺失或无 digest")

    url = download_url(repo, tag, meta.artifact_name)
    try:
        data = fetch(url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return errors + [f"下载失败 {url}：{exc}"]

    actual = hashlib.sha256(data).hexdigest()
    if digest is not None and actual != digest:
        errors.append(f"归档 sha256 与 release digest 不一致：{actual} != {digest}")

    manifest = extract_plugin_toml(data, meta.artifact_name)
    if manifest is None:
        return errors + [f"归档内未找到 plugin.toml：{meta.artifact_name}"]
    try:
        inner = tomllib.loads(manifest.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return errors + [f"归档内 plugin.toml 解析失败：{exc}"]
    if inner.get("name") != meta.name:
        errors.append(
            f"归档内 plugin.toml 的 name `{inner.get('name')}` 与元数据 name `{meta.name}` 不一致"
        )
    if normalize_version(str(inner.get("version", ""))) != version:
        errors.append(
            f"归档内 plugin.toml 的 version `{inner.get('version')}` 与元数据 version `{meta.version}` 不一致"
        )
    if inner.get("pluginapi") != meta.pluginapi:
        errors.append(
            f"归档内 plugin.toml 的 pluginapi `{inner.get('pluginapi')}` 与元数据 pluginapi `{meta.pluginapi}` 不一致"
        )
    return errors


def load_all() -> dict[str, PluginMeta]:
    """读取 plugins/ 下全部元数据（按文件名排序）。"""
    return {path.stem: load_plugin(path) for path in sorted(PLUGINS_DIR.glob("*.toml"))}


def build_entry(meta: PluginMeta) -> tuple[dict, list[str]]:
    """构造索引条目：字段对齐 plugin.toml，另加市场信息（download_url/sha256 等）。"""
    warnings: list[str] = []
    repo = repo_of(meta.repo_url)
    release = release_by_tag(repo, meta.version)
    tag = str(release.get("tag_name", ""))

    if normalize_version(tag) != normalize_version(meta.version):
        warnings.append(f"{meta.version}: release tag `{tag}` 与元数据 version 不一致")

    digest = asset_digest(release, meta.artifact_name)
    if digest is None:
        raise ValueError(
            f"{repo}: release `{tag}` 资产 `{meta.artifact_name}` 缺失或无 digest"
        )

    entry = {
        "name": meta.name,
        "version": meta.version,
        "description": meta.description,
        "authors": meta.authors,
        "license": meta.license,
        "repo_url": meta.repo_url,
        "url": meta.url,
        "pluginapi": meta.pluginapi,
        "artifact_name": meta.artifact_name,
        "download_url": download_url(repo, tag, meta.artifact_name),
        "sha256": digest,
    }
    return entry, warnings


def build_index(plugins: dict[str, PluginMeta]) -> tuple[dict, list[str]]:
    """汇总为索引；返回 (索引，警告列表)。"""
    entries, warnings = [], []
    for _name, meta in sorted(plugins.items()):
        entry, entry_warnings = build_entry(meta)
        entries.append(entry)
        warnings.extend(entry_warnings)
    index = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": entries,
    }
    return index, warnings
