"""BindDeckLite 上位机在线更新（上位机自更新 + 固件 OTA 在线检测）。

本模块只放「可单测的纯逻辑」与「网络 / 文件动作」；Flask 路由在 ``app.py``，
前端在 ``static/app.js``。分工与既有代码一致：``app.py`` 负责路由与设备状态，
这里负责版本比较、清单解析、下载校验与 exe 替换。

对外的主要入口：

* :func:`read_app_version` — 唯一版本源（``_version.py``）。
* :func:`version_is_newer` — 版本比较（数值元组，不看时间）。
* :func:`rolling_is_newer` — 滚动通道的构建标识比较（tag 时间戳 / app.build）。
* :func:`has_app_update` — 按通道判断某个 Release 是否比本地新。
* :func:`update_target_key` — 本次更新的目标标识（stable = 版本号，rolling = tag）。
* :func:`fetch_latest_release` — GitHub 正式 Release 查询（ETag / 304 / 60 秒防抖）。
* :func:`fetch_release_list` — GitHub Release 列表（rolling 通道用）。
* :func:`pick_rolling_release` — 从列表里选最新的 ``main-YYYYMMDD-HHMM`` 预发布。
* :func:`resolve_latest_update` — 解析 ``update.json``，缺失时回退到约定解析。
* :func:`download_to_file` / :func:`finalize_download` — 下载 + 校验（先落
  ``.part``，校验通过才改名，不会留下半成品 exe）。
* :func:`build_update_script` — 生成只接受固定路径的替换脚本。
* :func:`launch_update_script` — 用干净的环境（无 ``_PYI_*``）启动它。
* :func:`pyinstaller_clean_env` / :func:`pyinstaller_reset_scope` — 摆脱旧
  onefile 实例的环境（WS-576 自更新重启崩溃）。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import ntpath
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from i18n import t

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- 常量 -------------------------------------------------------------------

GITHUB_OWNER_REPO = "bbllmaster/BindDeckLite"
GITHUB_API_LATEST = "https://api.github.com/repos/bbllmaster/BindDeckLite/releases/latest"
#: 滚动通道：列表接口（``releases/latest`` 按定义排除 prerelease，查不到 nightly）。
GITHUB_API_RELEASES = ("https://api.github.com/repos/bbllmaster/BindDeckLite/releases"
                       "?per_page=30")
RELEASE_PAGE_URL = "https://github.com/bbllmaster/BindDeckLite/releases/latest"

#: 上位机更新通道（WS-557）：正式版 / main 滚动构建。
UPDATE_CHANNEL_STABLE = "stable"
UPDATE_CHANNEL_ROLLING = "rolling"
UPDATE_CHANNELS = (UPDATE_CHANNEL_STABLE, UPDATE_CHANNEL_ROLLING)
DEFAULT_UPDATE_CHANNEL = UPDATE_CHANNEL_STABLE

#: 滚动构建的 tag 形状（发布流水线按 UTC 生成）：``main-YYYYMMDD-HHMM``。
ROLLING_TAG_RE = re.compile(r"^main-(\d{8})-(\d{4})$")
#: 正式版 tag 必须以 ``v<数字>`` 开头；其余（含 main-*）一律不算正式版。
STABLE_TAG_RE = re.compile(r"^v\d")

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
#: 覆盖 exe 失败时的重试时长（秒，每秒一次）：扛过杀软 / 索引器的短暂锁定。
UPDATE_COPY_RETRY_SECONDS = 30
#: 等待上位机进程退出的上限（秒，每秒一次）。
UPDATE_PID_WAIT_SECONDS = 120
#: 替换脚本的日志文件名（与版本子目录同级，见 :func:`update_log_path`）。
UPDATE_LOG_NAME = "update.log"

#: ``update.log`` 的解码候选顺序（见 :func:`read_update_log`）。
#:
#: ``oem`` / ``mbcs`` 在 Windows 上是**单字节**代码页（US = cp437 / cp1252），
#: 对任意字节序列都不会抛 ``UnicodeDecodeError`` —— 排在前面就会把 cp936 的
#: 中文 ``copy`` 报错「成功」解成 ``ǔ▄ū°█└▒▒íú``，cp936 永远轮不到。所以
#: 多字节的中文页必须排在单字节兜底页之前（US runner 的 windows-exe 实测）。
UPDATE_LOG_ENCODINGS = ("utf-8", "cp936", "oem", "mbcs")

#: Windows 进程创建标志：``CREATE_NO_WINDOW`` 让更新脚本没有可见控制台
#: （用户点「立即更新」不再闪一个黑窗），``CREATE_NEW_PROCESS_GROUP`` 让脚本
#: 脱离上位机的进程组独立跑完。``DETACHED_PROCESS`` 与 ``CREATE_NO_WINDOW``
#: 互斥，不能一起给。
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

#: PyInstaller onefile 的私有环境变量前缀（``_PYI_APPLICATION_HOME_DIR`` /
#: ``_PYI_ARCHIVE_FILE`` / ``_PYI_PARENT_PROCESS_LEVEL`` 等）。
PYI_ENV_PREFIX = "_PYI_"
#: PyInstaller 官方的「当作全新实例启动」开关：bootloader 见到它就丢掉从
#: 父进程继承的全部 ``_PYI_*``、重新解包 —— 文档里专门给「派生出要活得比
#: 自己更久的子进程（例如重启自身）」用（WS-576）。
PYI_RESET_ENVIRONMENT = "PYINSTALLER_RESET_ENVIRONMENT"

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


def same_app_version(current: str, target: str) -> bool:
    """当前运行的是不是「更新目标那一版」（启动后确认更新成功用）。

    普通版本按 semver 比；**带构建元数据**（``0.0.0+main.<sha>``）时按完整
    字符串比 —— 两个 nightly 的 semver 元组都是 ``(0, 0, 0, 1)``，按元组比
    会把「回滚到旧 nightly」误判成更新成功。
    """
    current = str(current or "").strip()
    target = str(target or "").strip()
    if not target:
        return False
    if "+" in current or "+" in target:
        return current == target
    return version_tuple(current) == version_tuple(target)


def should_skip_version(current: str, latest: str, skipped: str) -> bool:
    """用户跳过的版本不再提示，直到出现更高版本。

    ``skipped`` 与 ``latest`` 相同 → 跳过；``latest`` 更高 → 重新提示。
    """
    if not skipped or not latest:
        return False
    if version_is_newer(latest, skipped):
        return False
    return True


# --- 更新通道与滚动构建比较（WS-557） ---------------------------------------

def normalize_channel(channel: str | None) -> str:
    """把配置里的通道值归一成 ``stable`` / ``rolling``（非法值按 stable）。"""
    value = str(channel or "").strip().lower()
    return value if value in UPDATE_CHANNELS else DEFAULT_UPDATE_CHANNEL


def rolling_tag_time(tag: str) -> int:
    """``main-20261109-0314`` → ``202611090314``（不可解析返回 0）。

    用整数比较而不是字符串：``20261109-0314`` 与 ``20261109-1314`` 只有
    按数值比才对；补齐的定长字符串其实也能比，但整数更直观。
    """
    match = ROLLING_TAG_RE.match(str(tag or "").strip())
    if not match:
        return 0
    try:
        return int(match.group(1) + match.group(2))
    except ValueError:
        return 0


def _iso_timestamp(value) -> float:
    """``2026-11-09T03:14:00Z`` → epoch 秒（解析不了返回 0）。"""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return 0.0
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def rolling_is_newer(remote_tag: str, remote_build: str = "",
                     local_tag: str = "", local_build: str = "") -> bool:
    """滚动通道：远端 Release 是否比本地更新。

    排序依据（不能再用 semver —— 同一 ``APP_VERSION`` 会对应多个 nightly）：

    1. tag 相同 → 不是更新（本地就是这一版）；
    2. 清单给了 ``app.build`` 且与本地 ``APP_BUILD`` 相同 → 不是更新
       （同一个提交构建出来的 exe）；
    3. 两边都能解析出 tag/构建里的时间戳 → 按时间戳比；
    4. 本地没有可比较的构建标识（老版本、手工构建、只有 sha） → 视为更新：
       远端是当前最新的滚动构建，先让用户升到它。
    """
    remote = str(remote_tag or "").strip()
    if not remote:
        return False
    if remote == str(local_tag or "").strip():
        return False
    remote_build = str(remote_build or "").strip()
    local_build = str(local_build or "").strip()
    if remote_build and local_build and remote_build == local_build:
        return False
    remote_time = rolling_tag_time(remote) or rolling_tag_time(remote_build)
    local_time = rolling_tag_time(local_tag) or rolling_tag_time(local_build)
    if remote_time and local_time:
        return remote_time > local_time
    return True


def has_app_update(channel: str, info: dict, current: str,
                   local_build: str = "", local_tag: str = "") -> bool:
    """按通道判断解析出的 Release 是否比本地新。

    * ``stable`` → semver（:func:`version_is_newer`，行为与 WS-523 一致）。
    * ``rolling`` → 构建标识（:func:`rolling_is_newer`）。
    """
    info = info or {}
    app = info.get("app") or {}
    if normalize_channel(channel) == UPDATE_CHANNEL_ROLLING:
        return rolling_is_newer(str(info.get("tag") or ""),
                                str(app.get("build") or ""),
                                local_tag, local_build)
    return version_is_newer(str(app.get("version") or ""), current)


def update_target_key(channel: str, info: dict) -> str:
    """本次更新的目标标识：stable 用版本号，rolling 用 tag。

    跳过某个 nightly 必须按 tag 记 —— 所有 nightly 的 semver 都是
    ``0.0.0+main.<sha>``，用版本号跳过会把后续每天的构建一起跳过。
    """
    info = info or {}
    if normalize_channel(channel) == UPDATE_CHANNEL_ROLLING:
        return str(info.get("tag") or "").strip()
    app = info.get("app") or {}
    return str(app.get("version") or "").strip()


def should_skip_update(channel: str, target_key: str, skipped: str) -> bool:
    """按通道判断是否跳过本次更新。

    * ``stable`` → 沿用 semver：出现更高版本重新提示。
    * ``rolling`` → 只按 tag 精确跳过：下一个 nightly 是新 tag，会重新提示。
    """
    skipped = str(skipped or "").strip()
    target = str(target_key or "").strip()
    if not skipped or not target:
        return False
    if normalize_channel(channel) == UPDATE_CHANNEL_ROLLING:
        return skipped == target
    return should_skip_version("", target, skipped)


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
    data = _fetch_github_json(api_url, timeout, force)
    return {"release": data["payload"], "from_cache": data["from_cache"],
            "not_modified": data["not_modified"]}


def fetch_release_list(api_url: str = GITHUB_API_RELEASES,
                       timeout: float = UPDATE_CHECK_TIMEOUT,
                       force: bool = False) -> dict:
    """查询 GitHub Release 列表（滚动通道用）。

    返回 ``{"releases": list, "from_cache": bool, "not_modified": bool}``。
    ETag / 304 / 60 秒防抖与 :func:`fetch_latest_release` 共用同一套缓存。
    """
    data = _fetch_github_json(api_url, timeout, force, expect_list=True)
    return {"releases": data["payload"], "from_cache": data["from_cache"],
            "not_modified": data["not_modified"]}


def _fetch_github_json(api_url: str, timeout: float, force: bool,
                       expect_list: bool = False) -> dict:
    """GET 一个 GitHub API 地址 → ``{payload, from_cache, not_modified}``。

    ``payload`` 是已解析的 JSON（``dict`` 或 ``list``）。两个 Release 查询
    接口共用缓存与错误映射，缓存键是完整 URL（含 ``?per_page=``）。
    """
    now = time.time()
    if not force:
        with _cache_lock:
            cached = _release_cache.get(api_url)
        if cached and now - cached["at"] < UPDATE_DEBOUNCE_SECONDS:
            return {"payload": cached["release"], "from_cache": True,
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
                return {"payload": cached["release"], "from_cache": True,
                        "not_modified": True}
            raise UpdateError(t("error.gh_not_modified"),
                              "E_NO_CACHE")
        if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            raise UpdateError(t("error.gh_rate_limited"),
                              "E_RATE_LIMIT")
        if exc.code == 404:
            raise UpdateError(t("error.gh_no_release"), "E_NOT_FOUND")
        raise UpdateError(t("error.gh_http") % exc.code, "E_HTTP_%d" % exc.code)
    except urllib.error.URLError as exc:
        raise UpdateError(t("error.gh_unreachable") % (exc.reason,), "E_NETWORK")
    except TimeoutError:
        raise UpdateError(t("error.gh_timeout"), "E_TIMEOUT")
    except OSError as exc:
        raise UpdateError(t("error.network") % (exc,), "E_NETWORK")

    try:
        payload = json.loads(raw)
    except ValueError:
        raise UpdateError(t("error.gh_bad_json"), "E_BAD_JSON")
    if expect_list:
        if not isinstance(payload, list):
            raise UpdateError(t("error.gh_bad_release"), "E_BAD_JSON")
    elif not isinstance(payload, dict):
        raise UpdateError(t("error.gh_bad_release"), "E_BAD_JSON")

    with _cache_lock:
        _release_cache[api_url] = {"release": payload, "at": now, "not_modified": False}
        if new_etag:
            _etag_cache[api_url] = new_etag
    return {"payload": payload, "from_cache": False, "not_modified": False}


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
        raise UpdateError(t("error.download_http") % (url.rsplit("/", 1)[-1], exc.code),
                          "E_HTTP_%d" % exc.code)
    except urllib.error.URLError as exc:
        raise UpdateError(t("error.gh_unreachable") % (exc.reason,), "E_NETWORK")
    except TimeoutError:
        raise UpdateError(t("error.gh_timeout"), "E_TIMEOUT")
    except OSError as exc:
        raise UpdateError(t("error.network") % (exc,), "E_NETWORK")

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
        raise UpdateError(t("error.release_manifest_bad_json"), "E_MANIFEST")
    if not isinstance(data, dict):
        raise UpdateError(t("error.release_manifest_bad_shape"), "E_MANIFEST")
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
        # WS-557: 滚动通道用构建标识比较版本；清单没有这个字段就是空串。
        "build": str(app.get("build") or "").strip(),
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


def resolve_latest_update(api_url: str = "",
                          timeout: float = UPDATE_CHECK_TIMEOUT,
                          force: bool = False,
                          channel: str = UPDATE_CHANNEL_STABLE,
                          releases_api_url: str = "") -> dict:
    """按通道查询 Release 并解析成更新信息（清单优先，回退兜底）。

    * ``stable`` → ``releases/latest``（GitHub 按定义排除 prerelease）。
    * ``rolling`` → ``releases?per_page=30`` 里筛 ``main-YYYYMMDD-HHMM``
      prerelease 取最新一条；找不到抛 ``E_NO_ROLLING``，**不回退 stable**
      （用户以为在测滚动版却拿到正式版比报错更糟）。

    ``api_url`` / ``releases_api_url`` 留空用模块常量（测试可覆盖常量）。
    """
    api_url = api_url or GITHUB_API_LATEST
    releases_api_url = releases_api_url or GITHUB_API_RELEASES
    channel = normalize_channel(channel)
    if channel == UPDATE_CHANNEL_ROLLING:
        data = fetch_release_list(releases_api_url, timeout=timeout, force=force)
        release = pick_rolling_release(data["releases"])
    else:
        data = fetch_latest_release(api_url, timeout=timeout, force=force)
        release = data["release"]
        tag = str(release.get("tag_name") or "").strip()
        # 正式通道只认 v* 正式发布；万一有人把 main-* 发成非预发布，也不能
        # 让稳定通道拿到滚动包。
        if tag and not STABLE_TAG_RE.match(tag):
            raise UpdateError(t("error.gh_no_stable_release"), "E_NO_STABLE")

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
    result["channel"] = channel
    result["not_modified"] = data.get("not_modified", False)
    result["from_cache"] = data.get("from_cache", False)
    return result


def pick_rolling_release(releases) -> dict:
    """从 Release 列表里挑最新一条 ``main-YYYYMMDD-HHMM`` 预发布。

    过滤条件：``prerelease == true``、非 draft、tag 严格匹配
    ``^main-\\d{8}-\\d{4}$``（正式 ``v*`` 与非滚动预发布都不算）。
    排序按 ``published_at``（缺失时 ``created_at``），再按 tag 时间戳兜底。

    一条都没有 → ``E_NO_ROLLING``（不静默回退 stable）。
    """
    best = None
    best_key = None
    for item in releases or []:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        if not item.get("prerelease"):
            continue
        tag = str(item.get("tag_name") or "").strip()
        if not ROLLING_TAG_RE.match(tag):
            continue
        key = (_iso_timestamp(item.get("published_at") or item.get("created_at")),
               rolling_tag_time(tag))
        if best is None or key > best_key:
            best, best_key = item, key
    if best is None:
        raise UpdateError(t("error.gh_no_rolling"), "E_NO_ROLLING")
    return best


# --- 下载与校验 -------------------------------------------------------------

def verify_downloaded_file(path: str, expected_sha256: str,
                           expected_size: int = 0, digest: str = "") -> str:
    """校验已下载文件；返回错误字符串（空串 = 通过）。

    校验大小与 SHA256（大小写不敏感）。**没有 sha256 一律不通过** ——
    清单里缺校验和时不自动更新。

    ``digest`` 是下载时边下边算出来的 SHA256；给了它就不再读一遍文件。
    """
    if not expected_sha256:
        return t("error.release_no_sha")
    try:
        actual_size = os.path.getsize(path)
    except OSError as exc:
        return t("error.download_read") % (exc,)
    if expected_size and actual_size != int(expected_size):
        return (t("error.download_size")
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
            return t("error.download_read") % (exc,)
        digest = digest.hexdigest()
    if digest.lower() != expected_sha256.strip().lower():
        return t("error.download_sha")
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
        raise UpdateError(t("error.download_empty_url"), "E_NO_URL")
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
                        raise UpdateError(t("error.download_cancelled"), "E_CANCELLED")
                    if time.monotonic() > deadline:
                        raise UpdateError(t("error.download_timeout"), "E_TIMEOUT")
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
        raise UpdateError(t("error.download_http_plain") % exc.code, "E_HTTP_%d" % exc.code)
    except urllib.error.URLError as exc:
        remove_quiet(part_path)
        raise UpdateError(t("error.gh_unreachable") % (exc.reason,), "E_NETWORK")
    except UpdateError:
        remove_quiet(part_path)
        raise
    except OSError as exc:
        remove_quiet(part_path)
        raise UpdateError(t("error.download_failed") % (exc,), "E_IO")

    if expected_size and downloaded != int(expected_size):
        remove_quiet(part_path)
        raise UpdateError(t("error.download_size")
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
        return t("error.download_save") % (exc,)
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
        raise UpdateError(t("error.update_illegal_path"), "E_BAD_PATH")
    return text


def batch_encoding(platform: str = "") -> str:
    """写 ``.bat`` 用的编码。

    cmd.exe 按**控制台 OEM 代码页**（CP_OEMCP）解析批处理文件，不是 ANSI
    代码页（CP_ACP）：``chcp`` 默认报的就是 OEM 页，cmd.exe 也用它来读批处理
    和写重定向。

    * 简体中文系统两个代码页都是 936（GBK），所以两种写法等价 —— 这也是
      旧实现用 ``mbcs`` 一直没出问题的原因。
    * 西文系统不等价（en-US：ANSI = 1252、OEM = 437；西欧 OEM = 850）。
      按 ANSI 落盘、cmd.exe 按 OEM 读，路径里的重音字符会被解成另一个字符，
      ``if not exist "%NEW%"`` 直接跳 ``:fail`` —— 此时上位机已经退出，
      用户的程序就消失了。

    所以 Windows 上必须用 ``oem``（= CP_OEMCP）。

    其它平台没有 ``oem``（只有 Windows 才有这个 codec），退回 UTF-8 ——
    上位机 exe 只在 Windows 上跑，这里只是让开发与测试环境可用。
    """
    name = platform or os.name
    return "oem" if name == "nt" else "utf-8"


def write_update_script(path: str, script: str, encoding: str = "") -> None:
    """把替换脚本落盘（编码见 :func:`batch_encoding`）。

    路径里有当前控制台代码页表示不了的字符时抛 :class:`UpdateError` ——
    与其写出一个 cmd 解析不了的脚本，不如明确告诉用户手动更新。
    """
    enc = encoding or batch_encoding()
    try:
        with open(path, "w", encoding=enc, newline="") as handle:
            handle.write(script)
    except UnicodeEncodeError:
        raise UpdateError(
            t("error.update_path_codepage"),
            "E_PATH_ENCODING")
    except OSError as exc:
        raise UpdateError(t("error.download_script") % (exc,), "E_IO")


def stage_update_script(script_path: str, script: str, marker_path: str) -> None:
    """准备好替换脚本：先清掉陈旧 marker，再落盘脚本。

    陈旧的 marker（上次 ``:fail`` 留下的）会让新脚本的 ``if exist
    "%MARKER%"`` 立刻判成功 —— 同版本重试时脚本直接跳过等待与回滚。
    """
    remove_quiet(marker_path)
    write_update_script(script_path, script)


def update_log_path(dest_dir: str) -> str:
    """替换脚本的日志路径：``...\\updates\\update.log``。

    放在版本子目录的**同级**：每次更新都覆盖同一份，用户反馈时只要这一个
    文件；版本子目录里的 ``.part`` / exe 会被清理，日志不该跟着丢。
    """
    directory = ntpath.dirname(str(dest_dir or "").rstrip("\\/"))
    return ntpath.join(directory, UPDATE_LOG_NAME) if directory else UPDATE_LOG_NAME


#: ``[result] <ok|fail|rollback> [reason=xxx]`` —— 脚本每次运行的最后结论。
_LOG_RESULT_RE = re.compile(r"^\[result\]\s+([a-z]+)(?:\s+reason=([\w.-]+))?")
#: 一行日志里「这是个失败」的迹象（脚本自己写的步骤行都是英文）。
_LOG_FAILURE_RE = re.compile(
    r"result=(?:failed|timeout|unknown)|unavailable|failed|refused|denied|cannot",
    re.IGNORECASE)


def _pick_log_detail(lines) -> str:
    """从日志里挑出最有用的一行失败说明。

    两步摘要（``[copy] step=replace result=failed ...``）与 ``copy`` 的原始
    输出（``拒绝访问。``）都要给用户看：前者说明哪一步，后者说明为什么。

    脚本自己的步骤行以 ``[`` 开头，``copy`` 的输出不是；所以从末尾往上找，
    第一条非括号行就是失败那一步的原始输出（copy 失败时它紧挨着摘要）。
    """
    summary = ""
    raw = ""
    for line in reversed(lines):
        if line.startswith("[result]"):
            continue
        if line.startswith("["):
            if not summary and _LOG_FAILURE_RE.search(line):
                summary = line
        elif not raw:
            raw = line
        if summary and raw:
            break
    if raw and summary:
        return "%s (%s)" % (summary, raw)
    return summary or raw or (lines[-1] if lines else "")


def parse_update_log(text: str) -> dict:
    """解析 ``update.log`` → ``{result, reason, detail, lines}``。

    * ``result``：``ok`` / ``fail`` / ``rollback``；日志被截断或没有结论时为空串；
    * ``reason``：机器可读的失败原因（``copy_failed`` / ``new_missing`` …）；
    * ``detail``：失败步骤摘要 + ``copy`` 的原始输出，给用户看；
    * ``lines``：非空行，调试与测试用。

    失败时**必须有 detail**：这才是本次修复要解决的事 —— 用户以前只看到
    「更新失败」，现在能看到具体是哪一步、为什么。
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    result = ""
    reason = ""
    for line in lines:
        match = _LOG_RESULT_RE.match(line)
        if not match:
            continue
        result = match.group(1)
        if result != "ok":
            reason = match.group(2) or reason
    detail = "" if result == "ok" else _pick_log_detail(lines)
    return {"result": result, "reason": reason, "detail": detail, "lines": lines}



def read_update_log(path: str, limit: int = 4000) -> dict:
    """读并解析 ``update.log``；读不到返回空结论，绝不抛异常。

    ``update.log`` 里既有脚本自己写的 ASCII 步骤行，也有 ``cmd`` 按**系统代码
    页**输出的 ``copy`` 报错（中文 Windows = cp936）。候选顺序见
    :data:`UPDATE_LOG_ENCODINGS`；全失败就用替换字符兜底 —— 解析日志失败
    不能再变成一个异常。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return {"result": "", "reason": "", "detail": "", "lines": []}
    text = ""
    for encoding in UPDATE_LOG_ENCODINGS:
        try:
            text = raw.decode(encoding)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    else:
        text = raw.decode("utf-8", "replace")
    if limit and len(text) > limit:
        text = text[-limit:]
    return parse_update_log(text)


def build_update_script(exe_path: str, new_exe: str, backup_exe: str,
                        marker_path: str, pid: int,
                        wait_seconds: int = UPDATE_RESTART_TIMEOUT,
                        log_path: str = "",
                        copy_retry_seconds: int = UPDATE_COPY_RETRY_SECONDS,
                        pid_wait_seconds: int = UPDATE_PID_WAIT_SECONDS) -> str:
    """生成替换脚本内容（Windows ``.bat``）。

    脚本只接受本次下载的固定路径（当前 exe / 新 exe / 备份 / 标记 / PID /
    日志），**不接受任意命令**。流程：

    1. 等当前进程（PID）退出（解析 ``tasklist /NH /FO CSV`` 的输出，
       ``tasklist`` 自己报错时**当作仍在运行**继续等，绝不抢跑覆盖）；
    2. 清掉只读属性与残留备份，把旧 exe 备份为 ``*.bak``；
    3. 新 exe 覆盖旧 exe，失败时每秒重试，最多 ``copy_retry_seconds`` 秒
       （杀软 / 索引器常把 exe 短暂锁住）；
    4. ``start /D <exe 目录>`` 启动新 exe；
    5. 等标记文件出现（最多 ``wait_seconds`` 秒）—— 出现即成功；
    6. 超时或替换失败才回滚 ``*.bak`` 并启动旧版。

    每一步都写 ``log_path``（含 ``errorlevel`` 与 ``copy`` 原始输出），上位机
    重启后读它给用户看具体失败步骤（WS-570）。

    只用 ``cmd`` 内建命令与 ``tasklist`` / ``find`` / ``ping``，不依赖
    PowerShell。

    脚本正文**只用 ASCII**（注释也是）：路径是唯一可能含非 ASCII 的部分，
    它按 cmd 的代码页落盘（见 :func:`write_update_script`）。脚本里出现
    中文注释会让英文 Windows 的代码页直接写不出来。
    """
    exe = _bat_quote(exe_path)
    new = _bat_quote(new_exe)
    backup = _bat_quote(backup_exe)
    marker = _bat_quote(marker_path)
    log = _bat_quote(log_path or ntpath.join(
        ntpath.dirname(marker_path), UPDATE_LOG_NAME))
    exe_dir = ntpath.dirname(exe_path) or "."
    pid_text = str(int(pid))
    retries = max(1, int(copy_retry_seconds))
    pid_waits = max(1, int(pid_wait_seconds))
    return "\r\n".join([
        "@echo off",
        "rem BindDeckLite self-update script (generated by the host, fixed paths only)",
        "rem Every step is appended to LOG; the host reads it back after restart.",
        "setlocal",
        'set "EXE=%s"' % exe,
        'set "EXE_DIR=%s"' % exe_dir,
        'set "NEW=%s"' % new,
        'set "BAK=%s"' % backup,
        'set "MARKER=%s"' % marker,
        'set "LOG=%s"' % log,
        'set "LIST=%LOG%.tasklist"',
        'set "PID=%s"' % pid_text,
        "set COUNT=0",
        "",
        "rem 1. start a fresh log -- a stale one must never explain this run.",
        'echo [start] pid=%PID% exe="%EXE%" >"%LOG%"',
        'echo [start] new="%NEW%" bak="%BAK%" >>"%LOG%"',
        "",
        "rem 2. wait for the host process to exit -- a running exe cannot be",
        "rem    overwritten. tasklist's own failure is NOT proof of exit: it used",
        "rem    to be read as 'process gone', so the script copied too early and",
        "rem    rolled back. Parse the PID column, and on any doubt keep waiting.",
        ":waitpid",
        'tasklist /FI "PID eq %PID%" /NH /FO CSV >"%LIST%" 2>NUL',
        "if errorlevel 1 goto pid_unknown",
        'find "%PID%" "%LIST%" >NUL',
        "if errorlevel 1 goto pid_gone",
        "goto pid_tick",
        "",
        ":pid_unknown",
        'echo [waitpid] tasklist unavailable, assume pid=%PID% still runs >>"%LOG%"',
        ":pid_tick",
        "ping -n 2 127.0.0.1 >NUL",
        "set /a COUNT+=1",
        "if %COUNT% LSS " + str(pid_waits) + " goto waitpid",
        'echo [waitpid] timeout after %COUNT%s, continue anyway >>"%LOG%"',
        "goto copyexe",
        "",
        ":pid_gone",
        'echo [waitpid] pid=%PID% exited after %COUNT%s >>"%LOG%"',
        "",
        "rem 3. drop the read-only flag and any stale backup, then back up.",
        ":copyexe",
        'if not exist "%NEW%" goto fail_new',
        'if exist "%BAK%" attrib -R "%BAK%" >>"%LOG%" 2>&1',
        'if exist "%BAK%" del /F /Q "%BAK%" >>"%LOG%" 2>&1',
        'if exist "%BAK%" goto fail_backup',
        'attrib -R "%EXE%" >>"%LOG%" 2>&1',
        'copy /Y "%EXE%" "%BAK%" >>"%LOG%" 2>&1',
        "if errorlevel 1 goto fail_backup",
        'echo [copy] step=backup result=ok >>"%LOG%"',
        "",
        "rem 4. replace the exe, retrying once a second: antivirus / the indexer",
        "rem    can hold it for a few seconds right after the host exits.",
        "set COUNT=0",
        ":retrycopy",
        'attrib -R "%EXE%" >>"%LOG%" 2>&1',
        'copy /Y "%NEW%" "%EXE%" >>"%LOG%" 2>&1',
        "if not errorlevel 1 goto copied",
        'echo [copy] step=replace result=failed attempt=%COUNT% errorlevel=%ERRORLEVEL% >>"%LOG%"',
        "ping -n 2 127.0.0.1 >NUL",
        "set /a COUNT+=1",
        "if %COUNT% LSS " + str(retries) + " goto retrycopy",
        'echo [copy] step=replace result=failed attempts=%COUNT% giving up >>"%LOG%"',
        'echo [result] rollback reason=copy_failed >>"%LOG%"',
        "goto rollback",
        "",
        ":copied",
        'echo [copy] step=replace result=ok attempts=%COUNT% >>"%LOG%"',
        "rem 5. start the new version with the exe directory as its CWD: relative",
        "rem    paths must not resolve inside the updates directory.",
        "rem WS-576: the old host was a onefile build and this script inherited",
        "rem its _PYI_* variables. A fresh bootloader would read the dead parent",
        "rem PID out of them and refuse to start, so reset them here as well.",
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"',
        'start "" /D "%EXE_DIR%" "%EXE%"',
        "if errorlevel 1 goto fail_start",
        'echo [start] new version launched >>"%LOG%"',
        "",
        "rem 6. wait for the marker the new version writes -- it means it started.",
        "set COUNT=0",
        ":waitnew",
        'if not exist "%MARKER%" goto waitnew_tick',
        'echo [waitnew] marker seen after %COUNT%s >>"%LOG%"',
        'echo [result] ok >>"%LOG%"',
        "goto done",
        ":waitnew_tick",
        "ping -n 2 127.0.0.1 >NUL",
        "set /a COUNT+=1",
        "if %COUNT% LSS " + str(int(wait_seconds)) + " goto waitnew",
        'echo [waitnew] result=timeout after %COUNT%s >>"%LOG%"',
        'echo [result] rollback reason=new_start_timeout >>"%LOG%"',
        "",
        "rem 7. roll back to the .bak and start the old version.",
        ":rollback",
        'if not exist "%BAK%" goto rollback_start',
        'attrib -R "%EXE%" >>"%LOG%" 2>&1',
        'copy /Y "%BAK%" "%EXE%" >>"%LOG%" 2>&1',
        'if errorlevel 1 echo [rollback] copy failed, exe may still be the new build >>"%LOG%"',
        ":rollback_start",
        'del /F /Q "%LIST%" >>"%LOG%" 2>&1',
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"',
        'start "" /D "%EXE_DIR%" "%EXE%"',
        "goto done",
        "",
        "rem Failed before the swap: the old exe is still in place -- start it again,",
        "rem otherwise the user is left with no program at all.",
        ":fail_new",
        'echo [copy] step=check result=failed reason=new_missing >>"%LOG%"',
        'echo [result] fail reason=new_missing >>"%LOG%"',
        "goto restart_old",
        "",
        ":fail_backup",
        'echo [copy] step=backup result=failed: cannot back up or clear the old exe >>"%LOG%"',
        'echo [result] fail reason=backup_failed >>"%LOG%"',
        "goto restart_old",
        "",
        ":fail_start",
        'echo [start] step=launch result=failed >>"%LOG%"',
        'echo [result] fail reason=new_start_failed >>"%LOG%"',
        "",
        ":restart_old",
        'del /F /Q "%LIST%" >>"%LOG%" 2>&1',
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"',
        'start "" /D "%EXE_DIR%" "%EXE%"',
        "",
        ":done",
        'echo [done] log="%LOG%" >>"%LOG%"',
        "endlocal",
        "",
    ])


def pyinstaller_clean_env(environ=None) -> dict:
    """返回一份去掉 PyInstaller onefile 私有变量的环境副本。

    PyInstaller 6.22.1 起，onefile 的「主应用子进程」会按继承来的顶层临时
    目录名里编码的 parent PID 去祖先进程树里找 onefile 父进程；找不到就直接
    退出（Windows 上是 ``Security validation failure: invalid originating
    onefile parent process (PID not found)!``）。自更新正好踩中这条链：

    旧 exe → ``cmd /c update.bat`` → ``start`` 新 exe

    三步都在继承旧实例的 ``_PYI_*``，而旧父进程此时已经退出。
    ``PYINSTALLER_RESET_ENVIRONMENT=1`` 是官方给出的开关：bootloader 见到它
    就把当前进程当作全新实例重新解包，不再核对一个已经死掉的父进程。

    其余变量（``PATH`` / ``SystemRoot`` ...）必须原样保留：``env=`` 是**整份
    替换**，漏掉 ``SystemRoot`` 会让子进程在 Windows 上直接起不来。
    """
    source = os.environ if environ is None else environ
    env = {key: value for key, value in source.items()
           if not key.startswith(PYI_ENV_PREFIX)}
    env[PYI_RESET_ENVIRONMENT] = "1"
    return env


@contextlib.contextmanager
def pyinstaller_reset_scope(environ=None):
    """临时把本进程环境里 onefile 的私有变量清掉（退出时还原）。

    只给「子进程没有 env 参数、只能继承父环境」的启动方式用
    （``ShellExecuteW("runas")`` 就是这种）。用完必须还原：UAC 被取消时本
    进程还要继续跑，它自己的 ``_PYI_*`` 不能就这么没了。
    """
    env = os.environ if environ is None else environ
    saved = {key: env[key] for key in list(env) if key.startswith(PYI_ENV_PREFIX)}
    had_reset = PYI_RESET_ENVIRONMENT in env
    previous_reset = env.get(PYI_RESET_ENVIRONMENT)
    try:
        for key in saved:
            del env[key]
        env[PYI_RESET_ENVIRONMENT] = "1"
        yield
    finally:
        if had_reset:
            env[PYI_RESET_ENVIRONMENT] = previous_reset
        else:
            env.pop(PYI_RESET_ENVIRONMENT, None)
        env.update(saved)


def launch_update_script(script_path: str) -> bool:
    """后台启动替换脚本（脱离当前进程的生命周期）。

    Windows 上用 ``CREATE_NO_WINDOW``：``DETACHED_PROCESS`` 时代用户会看到
    一个一闪而过的 CMD 窗口，失败信息全被吞掉（WS-570）。现在脚本没有可见
    控制台，诊断信息只走 ``update.log`` + 上位机日志。

    三个标准流全部指向 NUL：上位机退出后原句柄已失效，脚本不能再往里写。

    ``env`` 必须是 :func:`pyinstaller_clean_env` 的结果（WS-576）：脚本自己
    再 ``start`` 出来的新 exe 会继承它，带着旧实例的 ``_PYI_*`` 启动就会被
    PyInstaller 的 onefile 父进程校验直接拒掉。
    """
    try:
        kwargs = {"close_fds": True, "env": pyinstaller_clean_env()}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            kwargs["stdin"] = subprocess.DEVNULL
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
            command = ["cmd", "/c", script_path]
        else:
            kwargs["start_new_session"] = True
            command = ["/bin/sh", script_path]
        subprocess.Popen(command, **kwargs)
        return True
    except OSError:
        return False

