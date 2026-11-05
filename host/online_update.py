"""BindDeckLite 上位机在线更新（上位机自更新 + 固件 OTA 在线检测）。

本模块只放「可单测的纯逻辑」与「网络 / 文件动作」；Flask 路由在 ``app.py``，
前端在 ``static/app.js``。分工与既有代码一致：``app.py`` 负责路由与设备状态，
这里负责版本比较、清单解析、下载校验与 exe 替换。

对外的主要入口：

* :func:`read_app_version` — 唯一版本源（``_version.py``）。
* :func:`version_is_newer` — 版本比较（数值元组，不看时间）。
* :func:`fetch_latest_release` — GitHub 正式 Release 查询（ETag / 304 / 60 秒防抖）。
* :func:`resolve_latest_update` — 解析 ``update.json``，缺失时回退到约定解析。
* :func:`download_to_file` / :func:`finalize_download` — 下载 + 校验（先落
  ``.part``，校验通过才改名，不会留下半成品 exe）。
* :func:`build_update_script` — 生成只接受固定路径的替换脚本。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- 常量 -------------------------------------------------------------------

GITHUB_OWNER_REPO = "bbllmaster/BindDeckLite"
GITHUB_API_LATEST = "https://api.github.com/repos/bbllmaster/BindDeckLite/releases/latest"
RELEASE_PAGE_URL = "https://github.com/bbllmaster/BindDeckLite/releases/latest"

#: 上位机发布资产名（与 host/app.spec 的 EXE(name=...) 一致，发布页也是这个名字）。
APP_ASSET_NAME = "BindDeckLite.exe"
#: 发布清单资产名。
MANIFEST_ASSET_NAME = "update.json"
#: 校验和清单资产名（回退路径用它取 sha256）。
SUMS_ASSET_NAME = "SHA256SUMS.txt"

#: 开发时用环境变量覆盖版本号的变量名。
APP_VERSION_ENV = "BINDDECKLITE_APP_VERSION"
#: 没有任何版本信息时的兜底版本号。
DEFAULT_APP_VERSION = "0.0.0+dev"
#: 开发占位版本号：只有它才会被环境变量覆盖（发布版以 _version.py 为准）。
DEV_PLACEHOLDER_VERSION = "0.0.0+dev"

#: 检测请求超时（秒）。
UPDATE_CHECK_TIMEOUT = 10.0
#: 同 URL 的最短检测间隔（秒）：连点「检查更新」不重复打 GitHub。
UPDATE_DEBOUNCE_SECONDS = 60.0
#: 下载的总超时下限（秒），实际按文件大小动态放大。
UPDATE_DOWNLOAD_MIN_TIMEOUT = 300.0
#: 连续失败多少次后暂停自动更新。
UPDATE_FAIL_LIMIT = 2
#: 下载时每块读取大小。
DOWNLOAD_CHUNK = 64 * 1024
#: 替换脚本等待新版本启动的秒数。
UPDATE_RESTART_TIMEOUT = 60

#: 固件型号键 → 清单里 firmware 的 key。
FIRMWARE_VARIANTS = ("esp32dev", "esp32c3", "esp32c3_16mb")
#: 清单 firmware 的 key → 发布资产名里的 target 片段。
FIRMWARE_TARGET_OF_VARIANT = {
    "esp32dev": "esp32",
    "esp32c3": "c3",
    "esp32c3_16mb": "c3_16mb",
}


class UpdateError(Exception):
    """带可复制错误码的更新失败。

    ``code`` 形如 ``E_NETWORK`` / ``E_SHA256``，前端会把它显示给用户，方便反馈。
    """

    def __init__(self, message: str, code: str = "E_UPDATE"):
        super().__init__(message)
        self.message = message
        self.code = code


# --- 版本 -------------------------------------------------------------------

def _load_version_module():
    """加载 ``_version.py``（唯一版本源）。

    打包后 ``_version.py`` 被 PyInstaller 收进 PYZ（``app.spec`` 的
    ``hiddenimports`` 里显式列了它），此时用普通 import；未打包时按绝对路径
    加载，避免同名第三方模块干扰。
    """
    if getattr(sys, "frozen", False):
        try:
            import _version  # type: ignore

            return _version
        except Exception:  # noqa: BLE001
            return None
    path = os.path.join(SOURCE_DIR, "_version.py")
    try:
        spec = importlib.util.spec_from_file_location("binddecklite_version", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        return None


def read_app_version() -> str:
    """当前上位机版本号。

    顺序：``_version.py`` → 环境变量 ``BINDDECKLITE_APP_VERSION``（仅当
    ``_version.py`` 还是开发占位版时生效）→ ``0.0.0+dev``。
    """
    module = _load_version_module()
    version = str(getattr(module, "APP_VERSION", "") or "").strip() if module else ""
    if not version or version == DEV_PLACEHOLDER_VERSION:
        env = str(os.environ.get(APP_VERSION_ENV, "") or "").strip()
        if env:
            return env
    return version or DEFAULT_APP_VERSION


def read_app_build() -> str:
    """构建标识（发布流水线写入），没有就返回空串。"""
    module = _load_version_module()
    return str(getattr(module, "APP_BUILD", "") or "").strip() if module else ""


def version_tuple(version: str) -> tuple:
    """版本号 → 可比较的数值元组 ``(major, minor, patch, is_final)``。

    * ``1.10.0`` > ``1.9.9``（按数值，不按字符串）。
    * 预发布（带 ``-``）低于同号正式版：``1.5.0-rc1`` < ``1.5.0``。
    * ``+`` 之后的构建元数据不参与比较（``0.0.0+main.abc`` == ``0.0.0``）。
    """
    text = str(version or "").strip().lstrip("vV")
    if not text:
        return (0, 0, 0, 0)
    core = text.split("+", 1)[0]
    is_final = 1
    if "-" in core:
        core = core.split("-", 1)[0]
        is_final = 0
    numbers = []
    for part in core.split(".")[:3]:
        match = re.match(r"\d+", part.strip())
        numbers.append(int(match.group()) if match else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2], is_final)


def version_is_newer(new: str, old: str) -> bool:
    """``new`` 是否比 ``old`` 新（相等返回 False，不做降级）。"""
    return version_tuple(new) > version_tuple(old)


def version_at_least(version: str, minimum: str) -> bool:
    """``version`` 是否 >= ``minimum``（相等算满足）。"""
    if not minimum:
        return True
    return version_tuple(version) >= version_tuple(minimum)


def should_skip_version(current: str, latest: str, skipped: str) -> bool:
    """用户跳过的版本不再提示，直到出现更高版本。

    ``skipped`` 与 ``latest`` 相同 → 跳过；``latest`` 更高 → 重新提示。
    """
    if not skipped or not latest:
        return False
    if version_is_newer(latest, skipped):
        return False
    return True


# --- GitHub Release ---------------------------------------------------------

_cache_lock = threading.RLock()
#: url -> {"release": dict, "at": float, "not_modified": bool}
_release_cache: dict = {}
#: url -> etag
_etag_cache: dict = {}
#: url -> {"text": str, "at": float}
_text_cache: dict = {}


def reset_caches() -> None:
    """清空进程内的 Release / 文本缓存（测试与「强制重新检查」用）。"""
    with _cache_lock:
        _release_cache.clear()
        _etag_cache.clear()
        _text_cache.clear()


def _github_headers(extra: dict | None = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "BindDeckLite-Host",
    }
    token = str(os.environ.get("GITHUB_TOKEN", "") or "").strip()
    if token:
        # 只用来提高匿名限流上限；绝不进日志、文件或错误信息。
        headers["Authorization"] = "Bearer " + token
    if extra:
        headers.update(extra)
    return headers


def fetch_latest_release(api_url: str = GITHUB_API_LATEST,
                         timeout: float = UPDATE_CHECK_TIMEOUT,
                         force: bool = False) -> dict:
    """查询 GitHub 最新**正式** Release。

    返回 ``{"release": dict, "from_cache": bool, "not_modified": bool}``。

    * 带 ETag 的条件请求：远端没变时 GitHub 回 304，这里沿用上次结果，**不算错误**。
    * 同 URL 在 60 秒内重复调用直接返回上次结果，不发请求（连点防抖）。
    * ``GITHUB_TOKEN`` 可选，只加 ``Authorization`` 头，不落日志。

    失败时抛 :class:`UpdateError`（带错误码）。
    """
    now = time.time()
    if not force:
        with _cache_lock:
            cached = _release_cache.get(api_url)
        if cached and now - cached["at"] < UPDATE_DEBOUNCE_SECONDS:
            return {"release": cached["release"], "from_cache": True,
                    "not_modified": cached.get("not_modified", False)}

    headers = {}
    with _cache_lock:
        etag = _etag_cache.get(api_url)
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(api_url, headers=_github_headers(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            new_etag = response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            with _cache_lock:
                cached = _release_cache.get(api_url)
                if cached:
                    cached["at"] = now
                    cached["not_modified"] = True
            if cached:
                return {"release": cached["release"], "from_cache": True,
                        "not_modified": True}
            raise UpdateError("GitHub 返回 304 但本地没有缓存，请重新检查更新",
                              "E_NO_CACHE")
        if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            raise UpdateError("GitHub 访问频率超限（每小时匿名额度用尽），请稍后再试",
                              "E_RATE_LIMIT")
        if exc.code == 404:
            raise UpdateError("没有找到正式发布版本（GitHub 返回 404）", "E_NOT_FOUND")
        raise UpdateError("GitHub 返回 HTTP %d" % exc.code, "E_HTTP_%d" % exc.code)
    except urllib.error.URLError as exc:
        raise UpdateError("无法连接 GitHub：%s" % (exc.reason,), "E_NETWORK")
    except TimeoutError:
        raise UpdateError("连接 GitHub 超时，请检查网络或代理设置", "E_TIMEOUT")
    except OSError as exc:
        raise UpdateError("网络错误：%s" % (exc,), "E_NETWORK")

    try:
        release = json.loads(raw)
    except ValueError:
        raise UpdateError("GitHub 返回的内容不是合法 JSON", "E_BAD_JSON")
    if not isinstance(release, dict):
        raise UpdateError("GitHub 返回的 Release 格式异常", "E_BAD_JSON")

    with _cache_lock:
        _release_cache[api_url] = {"release": release, "at": now, "not_modified": False}
        if new_etag:
            _etag_cache[api_url] = new_etag
    return {"release": release, "from_cache": False, "not_modified": False}


def fetch_text(url: str, timeout: float = UPDATE_CHECK_TIMEOUT,
               cache_seconds: float = UPDATE_DEBOUNCE_SECONDS) -> str:
    """下载一个文本资产（``update.json`` / ``SHA256SUMS.txt``）。

    60 秒内重复下载同一 URL 直接用缓存，避免连点重复请求。
    """
    now = time.time()
    with _cache_lock:
        cached = _text_cache.get(url)
    if cached and now - cached["at"] < cache_seconds:
        return cached["text"]

    request = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise UpdateError("下载 %s 失败：HTTP %d" % (url.rsplit("/", 1)[-1], exc.code),
                          "E_HTTP_%d" % exc.code)
    except urllib.error.URLError as exc:
        raise UpdateError("无法连接 GitHub：%s" % (exc.reason,), "E_NETWORK")
    except TimeoutError:
        raise UpdateError("连接 GitHub 超时，请检查网络或代理设置", "E_TIMEOUT")
    except OSError as exc:
        raise UpdateError("网络错误：%s" % (exc,), "E_NETWORK")

    with _cache_lock:
        _text_cache[url] = {"text": text, "at": now}
    return text


def release_assets(release: dict) -> dict:
    """Release 资产表：``{资产名: {name, size, url}}``。"""
    assets = {}
    for item in (release or {}).get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        assets[name] = {
            "name": name,
            "size": int(item.get("size") or 0),
            "url": str(item.get("browser_download_url") or ""),
        }
    return assets


def _parse_sums(text: str) -> dict:
    """解析 ``SHA256SUMS.txt`` → ``{文件名: sha256}``。"""
    result = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip().lower()
        name = parts[-1].strip().lstrip("*")
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            result[name] = digest
    return result


def parse_manifest(text: str) -> dict:
    """解析 ``update.json``；格式不对抛 :class:`UpdateError`。"""
    try:
        data = json.loads(text)
    except ValueError:
        raise UpdateError("发布清单 update.json 不是合法 JSON", "E_MANIFEST")
    if not isinstance(data, dict):
        raise UpdateError("发布清单 update.json 格式异常", "E_MANIFEST")
    return data


def pick_app_asset(manifest: dict) -> dict | None:
    """从 ``update.json`` 里取上位机资产条目；缺失返回 ``None``。"""
    app = (manifest or {}).get("app")
    if not isinstance(app, dict):
        return None
    version = str(app.get("version") or "").strip()
    asset = str(app.get("asset") or "").strip()
    if not version or not asset:
        return None
    try:
        size = int(app.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "version": version,
        "asset": asset,
        "sha256": str(app.get("sha256") or "").strip().lower(),
        "size": size,
        "min_from": str(app.get("min_from") or "").strip(),
    }


def pick_firmware_asset(manifest: dict, variant: str) -> dict | None:
    """从 ``update.json`` 里取某个型号的固件条目；缺失返回 ``None``。"""
    firmware = (manifest or {}).get("firmware")
    if not isinstance(firmware, dict):
        return None
    item = firmware.get(variant)
    if not isinstance(item, dict):
        return None
    version = str(item.get("version") or "").strip()
    asset = str(item.get("asset") or "").strip()
    if not version or not asset:
        return None
    try:
        size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "version": version,
        "asset": asset,
        "sha256": str(item.get("sha256") or "").strip().lower(),
        "size": size,
    }


def variant_for_device(chip: str, flash_kb) -> str | None:
    """设备型号 → 清单里的 firmware key；无法确定时返回 ``None``（不猜）。

    判定同时看 ``chip`` 与 flash 容量：

    * ``flash_kb <= 0``（老固件缺第 5 个 VERSION 字段）→ 不知道，不猜。
    * ``flash_kb < 4000``（2 MB 模块，没有 app1 分区）→ 没有在线固件。
    * ``chip == "ESP32"`` → ``esp32dev``。
    * ``chip == "C3"`` 且 ``flash_kb >= 16000`` → ``esp32c3_16mb``。
    * ``chip == "C3"`` 且 ``flash_kb >= 4000`` → ``esp32c3``。
    """
    try:
        flash = int(flash_kb or 0)
    except (TypeError, ValueError):
        flash = 0
    if flash <= 0:
        return None
    if flash < 4000:
        return None
    name = str(chip or "").strip().upper()
    if name == "ESP32":
        return "esp32dev"
    if name == "C3":
        return "esp32c3_16mb" if flash >= 16000 else "esp32c3"
    return None


def version_from_asset_name(name: str) -> str:
    """``ota_binddecklite_c3_v1.3.0.bin`` → ``1.3.0``。"""
    match = re.search(r"_v([0-9][0-9A-Za-z.\-]*)\.bin$", name or "", re.IGNORECASE)
    return match.group(1) if match else ""


def resolve_release_update(release: dict, manifest: dict | None = None,
                            sums_text: str = "") -> dict:
    """把一个 Release（+ 可选清单）解析成更新信息。

    有 ``update.json`` → ``source="manifest"``；没有 → 回退按 tag 版本 +
    资产名约定解析（``source="fallback"``，兼容旧 Release）。

    返回 ``{source, app, firmware, notes, published_at, release_url, tag}``；
    ``app`` 为 ``None`` 表示拿不到可用的上位机资产。
    """
    release = release or {}
    assets = release_assets(release)
    tag = str(release.get("tag_name") or "").strip()
    notes = str(release.get("body") or "")
    published_at = str(release.get("published_at") or "")
    release_url = str(release.get("html_url") or RELEASE_PAGE_URL)

    app_entry = pick_app_asset(manifest) if manifest else None
    firmware = {}
    source = "fallback"

    if manifest:
        source = "manifest"
        for variant in FIRMWARE_VARIANTS:
            item = pick_firmware_asset(manifest, variant)
            if item:
                firmware[variant] = item
        notes = str(manifest.get("notes") or notes)
        published_at = str(manifest.get("published_at") or published_at)
    else:
        # 回退：tag 去掉 v 就是版本号，资产名按约定。
        fallback_version = tag[1:] if tag.lower().startswith("v") else tag
        sums = _parse_sums(sums_text)
        app_entry = {
            "version": fallback_version,
            "asset": APP_ASSET_NAME,
            "sha256": sums.get(APP_ASSET_NAME, ""),
            "size": assets.get(APP_ASSET_NAME, {}).get("size", 0),
            "min_from": "",
        }
        # 固件条目按资产名里的 target 反推型号。
        for variant, target in FIRMWARE_TARGET_OF_VARIANT.items():
            for name, info in assets.items():
                if not name.startswith("ota_binddecklite_%s_v" % target):
                    continue
                version = version_from_asset_name(name)
                if not version:
                    continue
                firmware[variant] = {
                    "version": version,
                    "asset": name,
                    "sha256": sums.get(name, ""),
                    "size": info.get("size", 0),
                }
                break

    if app_entry:
        info = assets.get(app_entry["asset"])
        if info:
            app_entry["size"] = app_entry.get("size") or info.get("size", 0)
            app_entry["url"] = info.get("url", "")
        else:
            app_entry["url"] = ""
    else:
        app_entry = None

    for variant, item in firmware.items():
        info = assets.get(item["asset"])
        item["url"] = info.get("url", "") if info else ""

    return {
        "source": source,
        "app": app_entry,
        "firmware": firmware,
        "notes": notes,
        "published_at": published_at,
        "release_url": release_url or RELEASE_PAGE_URL,
        "tag": tag,
    }


def resolve_latest_update(api_url: str = GITHUB_API_LATEST,
                          timeout: float = UPDATE_CHECK_TIMEOUT,
                          force: bool = False) -> dict:
    """查询最新正式 Release 并解析成更新信息（清单优先，回退兜底）。"""
    data = fetch_latest_release(api_url, timeout=timeout, force=force)
    release = data["release"]
    assets = release_assets(release)

    manifest = None
    manifest_asset = assets.get(MANIFEST_ASSET_NAME)
    if manifest_asset and manifest_asset.get("url"):
        try:
            manifest = parse_manifest(fetch_text(manifest_asset["url"], timeout))
        except UpdateError:
            manifest = None

    sums_text = ""
    if manifest is None:
        sums_asset = assets.get(SUMS_ASSET_NAME)
        if sums_asset and sums_asset.get("url"):
            try:
                sums_text = fetch_text(sums_asset["url"], timeout)
            except UpdateError:
                sums_text = ""

    result = resolve_release_update(release, manifest, sums_text)
    result["not_modified"] = data.get("not_modified", False)
    result["from_cache"] = data.get("from_cache", False)
    return result


# --- 下载与校验 -------------------------------------------------------------

def verify_downloaded_file(path: str, expected_sha256: str,
                           expected_size: int = 0, digest: str = "") -> str:
    """校验已下载文件；返回错误字符串（空串 = 通过）。

    校验大小与 SHA256（大小写不敏感）。**没有 sha256 一律不通过** ——
    清单里缺校验和时不自动更新。

    ``digest`` 是下载时边下边算出来的 SHA256；给了它就不再读一遍文件。
    """
    if not expected_sha256:
        return "发布清单缺少 SHA256 校验值，不能自动更新"
    try:
        actual_size = os.path.getsize(path)
    except OSError as exc:
        return "无法读取下载文件：%s" % (exc,)
    if expected_size and actual_size != int(expected_size):
        return ("文件大小不符（清单 %d 字节，实际 %d 字节）"
                % (int(expected_size), actual_size))
    if not digest:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            return "无法读取下载文件：%s" % (exc,)
        digest = digest.hexdigest()
    if digest.lower() != expected_sha256.strip().lower():
        return "SHA256 校验失败，文件已损坏或被篡改"
    return ""


def update_dir(version: str) -> str:
    """下载目录：``%LOCALAPPDATA%\\BindDeckLite\\updates\\<version>``。

    非 Windows 或取不到 ``LOCALAPPDATA`` 时退到临时目录。
    """
    base = ""
    if os.name == "nt":
        base = str(os.environ.get("LOCALAPPDATA", "") or "")
    if not base:
        base = os.path.join(tempfile.gettempdir(), "BindDeckLite")
    path = os.path.join(base, "updates", str(version or "unknown"))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = os.path.join(tempfile.gettempdir(), "BindDeckLite", "updates",
                            str(version or "unknown"))
        os.makedirs(path, exist_ok=True)
    return path


def download_to_file(url: str, dest_dir: str, filename: str,
                     progress=None, cancel=None,
                     timeout: float | None = None,
                     expected_size: int = 0,
                     hasher=None) -> str:
    """把资产下载到 ``<dest_dir>/<filename>.part``，返回临时文件路径。

    只下载，不改名 —— 校验由 :func:`finalize_download` 负责，所以中途失败
    绝不会留下一个「看起来像 exe」的半成品。

    ``hasher`` 传入一个 hashlib 对象即可**边下边算** SHA256：校验时不用把
    文件再读一遍（验收 2 的「边下边算」）。
    """
    if not url:
        raise UpdateError("下载地址为空", "E_NO_URL")
    os.makedirs(dest_dir, exist_ok=True)
    part_path = os.path.join(dest_dir, filename + ".part")

    if timeout is None:
        timeout = UPDATE_DOWNLOAD_MIN_TIMEOUT
        if expected_size:
            # 保守按 60 KB/s 估算，再留出至少 5 分钟。
            timeout = max(UPDATE_DOWNLOAD_MIN_TIMEOUT, expected_size / 60.0 / 1024.0 + 60.0)
    deadline = time.monotonic() + timeout

    total = 0
    downloaded = 0
    request = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(request, timeout=min(timeout, 60.0)) as response:
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else int(expected_size or 0)
            with open(part_path, "wb") as handle:
                while True:
                    if cancel is not None and cancel():
                        raise UpdateError("已取消下载", "E_CANCELLED")
                    if time.monotonic() > deadline:
                        raise UpdateError("下载超时，请检查网络后重试", "E_TIMEOUT")
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
    except urllib.error.HTTPError as exc:
        remove_quiet(part_path)
        raise UpdateError("下载失败：HTTP %d" % exc.code, "E_HTTP_%d" % exc.code)
    except urllib.error.URLError as exc:
        remove_quiet(part_path)
        raise UpdateError("无法连接 GitHub：%s" % (exc.reason,), "E_NETWORK")
    except UpdateError:
        remove_quiet(part_path)
        raise
    except OSError as exc:
        remove_quiet(part_path)
        raise UpdateError("下载失败：%s" % (exc,), "E_IO")

    if expected_size and downloaded != int(expected_size):
        remove_quiet(part_path)
        raise UpdateError("文件大小不符（清单 %d 字节，实际 %d 字节）"
                          % (int(expected_size), downloaded), "E_SIZE")
    return part_path


def finalize_download(part_path: str, final_path: str, expected_sha256: str,
                       expected_size: int = 0, digest: str = "") -> str:
    """校验临时文件；通过则改名为正式文件，返回错误字符串（空串 = 成功）。

    校验不通过时删除临时文件 —— 不留半成品，也不自动重试循环。

    ``digest`` 是下载时**边下边算**得到的 SHA256：给了它就不用把文件再读
    一遍。没给（比如手动准备的文件）才回退到读文件算。
    """
    error = verify_downloaded_file(part_path, expected_sha256, expected_size,
                                   digest=digest)
    if error:
        remove_quiet(part_path)
        return error
    try:
        os.replace(part_path, final_path)
    except OSError as exc:
        remove_quiet(part_path)
        return "保存下载文件失败：%s" % (exc,)
    return ""


def download_asset(url: str, dest_dir: str, filename: str,
                   expected_sha256: str = "", expected_size: int = 0,
                   progress=None, cancel=None,
                   timeout: float | None = None) -> str:
    """下载 + 校验一步到位，返回落地路径（失败抛 :class:`UpdateError`）。"""
    part_path = download_to_file(url, dest_dir, filename, progress=progress,
                                 cancel=cancel, timeout=timeout,
                                 expected_size=expected_size)
    final_path = os.path.join(dest_dir, filename)
    error = finalize_download(part_path, final_path, expected_sha256, expected_size)
    if error:
        raise UpdateError(error, "E_SHA256" if "SHA256" in error else "E_SIZE")
    return final_path


def remove_quiet(path: str) -> None:
    """删除文件，失败不抛（清理路径上不该因为删不掉而中断流程）。"""
    try:
        os.remove(path)
    except OSError:
        pass


# --- 替换与重启 -------------------------------------------------------------

def is_writable_dir(path: str) -> bool:
    """目录是否可写（写一个探测文件再删掉）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".bdl-write-test")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _bat_quote(value: str) -> str:
    """把路径安全地放进批处理：拒绝会破坏脚本的字符。

    ``%`` 会被 cmd 当成变量引用（``%PATH%`` 之类）展开，路径里有它就一定
    出错 —— 直接拒绝，不生成脚本。
    """
    text = str(value or "")
    if '"' in text or "\n" in text or "\r" in text or "%" in text:
        raise UpdateError("路径包含非法字符，已中止更新", "E_BAD_PATH")
    return text


def batch_encoding(platform: str = "") -> str:
    """写 ``.bat`` 用的编码。

    cmd.exe 按**系统 ANSI 代码页**解析批处理文件：UTF-8 落盘时中文用户
    名路径会被解成乱码（``set "EXE=C:\\Users\\张三\\..."`` → ``寮犱笁``），
    ``if not exist "%NEW%"`` 直接跳 ``:fail``。所以 Windows 上必须用
    ``mbcs``（= 当前系统 ANSI 代码页，简体中文 = 936）。

    其它平台没有 ``mbcs``（只有 Windows 才有这个 codec），退回 UTF-8 ——
    上位机 exe 只在 Windows 上跑，这里只是让开发与测试环境可用。
    """
    name = platform or os.name
    return "mbcs" if name == "nt" else "utf-8"


def write_update_script(path: str, script: str, encoding: str = "") -> None:
    """把替换脚本落盘（编码见 :func:`batch_encoding`）。

    路径里有当前系统代码页表示不了的字符时抛 :class:`UpdateError` ——
    与其写出一个 cmd 解析不了的脚本，不如明确告诉用户手动更新。
    """
    enc = encoding or batch_encoding()
    try:
        with open(path, "w", encoding=enc, newline="") as handle:
            handle.write(script)
    except UnicodeEncodeError:
        raise UpdateError(
            "当前系统代码页无法表示安装路径中的字符，请手动下载新版本",
            "E_PATH_ENCODING")
    except OSError as exc:
        raise UpdateError("写入更新脚本失败：%s" % (exc,), "E_IO")


def stage_update_script(script_path: str, script: str, marker_path: str) -> None:
    """准备好替换脚本：先清掉陈旧 marker，再落盘脚本。

    陈旧的 marker（上次 ``:fail`` 留下的）会让新脚本的 ``if exist
    "%MARKER%"`` 立刻判成功 —— 同版本重试时脚本直接跳过等待与回滚。
    """
    remove_quiet(marker_path)
    write_update_script(script_path, script)


def build_update_script(exe_path: str, new_exe: str, backup_exe: str,
                        marker_path: str, pid: int,
                        wait_seconds: int = UPDATE_RESTART_TIMEOUT) -> str:
    """生成替换脚本内容（Windows ``.bat``）。

    脚本只接受本次下载的固定路径（当前 exe / 新 exe / 备份 / 标记 / PID），
    **不接受任意命令**。流程：

    1. 等当前进程（PID）退出（运行中的 exe 不能被覆盖）；
    2. 旧 exe 备份为 ``*.bak``；
    3. 新 exe 覆盖旧 exe；
    4. 启动新 exe；
    5. 等标记文件出现（最多 ``wait_seconds`` 秒）—— 出现即新版本已启动，
       脚本退出；
    6. 超时则回滚 ``*.bak`` 并启动旧版。

    只用 ``cmd`` 内建命令与 ``ping`` 延时，不依赖 PowerShell。

    脚本正文**只用 ASCII**（注释也是）：路径是唯一可能含非 ASCII 的部分，
    它按 cmd 的代码页落盘（见 :func:`write_update_script`）。脚本里出现
    中文注释会让英文 Windows 的代码页直接写不出来。
    """
    exe = _bat_quote(exe_path)
    new = _bat_quote(new_exe)
    backup = _bat_quote(backup_exe)
    marker = _bat_quote(marker_path)
    pid_text = str(int(pid))
    return "\r\n".join([
        "@echo off",
        "rem BindDeckLite self-update script (generated by the host, fixed paths only)",
        "setlocal",
        'set "EXE=%s"' % exe,
        'set "NEW=%s"' % new,
        'set "BAK=%s"' % backup,
        'set "MARKER=%s"' % marker,
        'set "PID=%s"' % pid_text,
        "set COUNT=0",
        "",
        "rem 1. wait for the host process to exit -- a running exe cannot be overwritten.",
        "rem    (no parenthesis block: %COUNT% inside one is not re-expanded)",
        ":waitpid",
        'tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL',
        "if errorlevel 1 goto copyexe",
        "ping -n 2 127.0.0.1 >NUL",
        "set /a COUNT+=1",
        "if %COUNT% LSS 120 goto waitpid",
        "",
        "rem 2. back up the old exe, then overwrite it.",
        ":copyexe",
        'if not exist "%NEW%" goto fail',
        'copy /Y "%EXE%" "%BAK%" >NUL 2>&1',
        "if errorlevel 1 goto fail",
        'copy /Y "%NEW%" "%EXE%" >NUL 2>&1',
        "if errorlevel 1 goto rollback",
        'start "" "%EXE%"',
        "",
        "rem 3. wait for the marker the new version writes -- it means it started.",
        "set COUNT=0",
        ":waitnew",
        'if exist "%MARKER%" goto done',
        "ping -n 2 127.0.0.1 >NUL",
        "set /a COUNT+=1",
        "if %COUNT% LSS " + str(int(wait_seconds)) + " goto waitnew",
        "",
        "rem 4. timed out: roll back to the .bak and start the old version.",
        ":rollback",
        'copy /Y "%BAK%" "%EXE%" >NUL 2>&1',
        'start "" "%EXE%"',
        "goto done",
        "",
        "rem Failed before the swap: the old exe is still in place -- start it again,",
        "rem otherwise the user is left with no program at all.",
        ":fail",
        'start "" "%EXE%"',
        "",
        ":done",
        "endlocal",
        "",
    ])


def launch_update_script(script_path: str) -> bool:
    """后台启动替换脚本（脱离当前进程的生命周期）。"""
    try:
        kwargs = {"close_fds": True}
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
            kwargs["creationflags"] = flags
            command = ["cmd", "/c", script_path]
        else:
            kwargs["start_new_session"] = True
            command = ["/bin/sh", script_path]
        subprocess.Popen(command, **kwargs)
        return True
    except OSError:
        return False
