"""WS-523: 发布流水线的契约测试。

这些断言读的是**真实的** ``.github/workflows/release.yml`` 与 ``tools/`` 脚本，
改坏流水线时这里会红 —— 流水线出问题时，最贵的不是构建失败，而是发出去的
Release 少了「本次更新」小节、或清单里的 SHA256 与实际文件对不上。

跑法：``pytest host/test_release_pipeline.py -q``
"""

import hashlib
import json
import locale
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_manifest  # noqa: E402
import release_notes  # noqa: E402
import release_version  # noqa: E402

RELEASE_YML = os.path.join(ROOT, ".github", "workflows", "release.yml")
BUILD_HOST_YML = os.path.join(ROOT, ".github", "workflows", "build-host.yml")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _run_pipeline_script(args):
    """跑一个发布脚本（``tools/`` 下），并按 UTF-8 收发它的输出。

    Windows runner 的区域编码是 ANSI 代码页（en-US → cp1252），不是 UTF-8：
    子进程写 stderr 时用的是代码页，中文编不进去，而 stderr 的默认错误处理
    是 ``backslashreplace``，于是中文被写成 ``\\u5347\\u7ea7\\u63d0\\u793a``
    字面量 —— 对 stderr 的中文断言就假失败了。WS-532 就是这么红的
    （门禁本身没坏：退出码仍是 1，坏的是那条子串断言）。

    这里把**两端**都钉成 UTF-8：子进程用 ``PYTHONIOENCODING=utf-8`` 写，
    父进程用 ``encoding="utf-8"`` 解。只改一端不够：写的若是代码页、读的
    是 utf-8（或反过来）一样是乱码。

    用 ``PYTHONIOENCODING`` 而不是 ``PYTHONUTF8=1``：后者会把子进程的**文件
    系统编码**也换成 utf-8，那是对「标准流编码」这个问题的过度改动；这里要
    修的只是 stdout/stderr。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable] + list(args), capture_output=True,
                          text=True, encoding="utf-8", env=env)


# --- release.yml 的结构 ------------------------------------------------------

def test_the_host_job_injects_the_version_before_pyinstaller():
    """版本必须进到 host/_version.py，且在打包之前 —— 否则 exe 里没有版本号。"""
    text = _read(RELEASE_YML)
    assert "tools/release_version.py" in text
    assert "--write-version-file host/_version.py" in text
    assert text.index("Resolve release version") < text.index("pyinstaller --noconfirm")
    # host/_version.py 必须是 hiddenimport（它在函数里 import，静态分析不可靠）。
    spec = _read(os.path.join(ROOT, "host", "app.spec"))
    assert '"_version"' in spec


def test_the_publish_job_reuses_the_exe_version():
    """update.json 里的版本必须与 exe 里的同源 —— 不能各算各的。

    WS-571 把版本改成两步传递：发布 job 先由 ``needs.host.outputs.version``
    （host 没重建时退回上一版 ``update.json`` 的 ``app.version``）解析出
    ``steps.appver.outputs.APP_VERSION``，manifest 与 Release 都用它。链路的
    起点仍然是 exe 那一份版本号，所以两段都要断言。
    """
    text = _read(RELEASE_YML)
    assert "version: ${{ steps.version.outputs.VERSION }}" in text
    assert "HOST_VERSION: ${{ needs.host.outputs.version }}" in text
    assert "APP_VERSION: ${{ steps.appver.outputs.APP_VERSION }}" in text


def test_the_manifest_is_built_from_the_published_files():
    """清单必须由脚本从**实际发布文件**算出 size/sha256，不许手写。"""
    text = _read(RELEASE_YML)
    assert "tools/build_manifest.py" in text
    assert "--assets artifacts" in text
    # update.json 必须随 Release 一起上传（上位机从 Release 读它）。
    assert "artifacts/update.json" in text


def test_the_release_body_gate_is_present():
    """正文缺「本次更新」→ publish job 必须失败，不发 Release（WS-523 验收 11）。"""
    text = _read(RELEASE_YML)
    assert "tools/release_notes.py" in text
    assert 'grep -q "^## 本次更新"' in text
    # 门禁必须在 gh release create 之前。
    assert text.index("grep -q") < text.index("gh release create")


def test_the_new_tests_run_in_ci():
    """CI 跑的是显式文件列表 —— 新测试文件必须列进去，否则永远不跑。"""
    text = _read(BUILD_HOST_YML)
    assert "test_app_update.py" in text
    assert "test_fw_update.py" in text


def test_every_front_end_test_runs_in_ci():
    """每个 ``host/test_*.mjs`` 都必须被某个 CI 作业跑到。

    没被列进去的测试文件等于不存在：本地跑绿了，CI 照样放过回归
    （WS-480 就是这么漏的）。这里直接扫目录，不靠人肉维护清单。
    """
    workflows_dir = os.path.join(ROOT, ".github", "workflows")
    ci_text = "".join(
        _read(os.path.join(workflows_dir, name))
        for name in sorted(os.listdir(workflows_dir))
        if name.endswith((".yml", ".yaml"))
    )
    host_dir = os.path.join(ROOT, "host")
    scripts = sorted(
        name for name in os.listdir(host_dir)
        if name.startswith("test_") and name.endswith(".mjs")
    )
    assert scripts, "host/ 下应该还有 .mjs 前端测试"
    for name in scripts:
        assert name in ci_text, "%s 没有被任何 CI 作业跑到" % name


# --- 版本语义 ----------------------------------------------------------------

def test_tag_versions():
    info = release_version.parse_ref("refs/tags/v1.4.0", "abcdef1234")
    assert info["version"] == "1.4.0"
    assert info["formal"] is True
    assert info["prerelease"] is False


def test_prerelease_tags_are_prereleases():
    info = release_version.parse_ref("refs/tags/v1.5.0-rc1", "abcdef1234")
    assert info["version"] == "1.5.0-rc1"
    assert info["prerelease"] is True


def test_main_builds_are_rolling_prereleases():
    info = release_version.parse_ref("refs/heads/main", "abcdef1234")
    assert info["version"] == "0.0.0+main.abcdef1"
    assert info["formal"] is False
    assert info["prerelease"] is True


def test_the_version_file_is_written_with_the_derived_version(tmp_path):
    path = tmp_path / "_version.py"
    release_version.write_version_file(str(path), "1.4.0", "abc1234")
    text = path.read_text(encoding="utf-8")
    assert 'APP_VERSION = "1.4.0"' in text
    assert 'APP_BUILD = "abc1234"' in text


# --- 「本次更新」小节的校验 ---------------------------------------------------

def test_valid_notes_pass_the_gate():
    text = ("## 本次更新\n\n### 新增\n- x\n\n### 修复\n- 无\n\n"
            "### 变更 / 不兼容\n- 无\n\n### 升级提示\n- 无\n")
    assert release_notes.validate_notes(text) == []


def test_notes_without_the_heading_fail_the_gate():
    text = "### 新增\n- x\n### 修复\n- 无\n### 变更\n- 无\n### 升级提示\n- 无\n"
    missing = release_notes.validate_notes(text)
    assert "## 本次更新" in missing


def test_notes_missing_a_subsection_fail_the_gate():
    text = "## 本次更新\n\n### 新增\n- x\n\n### 修复\n- 无\n"
    missing = release_notes.validate_notes(text)
    assert "### 变更" in missing
    assert "### 升级提示" in missing


def test_the_changelog_summary_has_the_full_structure(tmp_path):
    """滚动包从 CHANGELOG 汇总出来的内容也必须结构完整。"""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## 某个新功能 — 2026-11-05（WS-526）\n\n正文\n\n"
        "## v1.3.0 — 2026-10-19\n\n### 固件\n\n- 旧内容\n", encoding="utf-8")
    summary = release_notes.changelog_summary(str(changelog))
    assert release_notes.validate_notes(summary) == []
    assert "某个新功能" in summary
    assert "旧内容" not in summary, "已发布版本的内容不该混进下一次发布"


def test_a_formal_tag_without_notes_resolves_to_nothing(tmp_path):
    """正式 tag 找不到任何来源 → 解析为空 → 门禁失败（不发版）。"""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## v1.3.0 — 2026-10-19\n\nx\n",
                         encoding="utf-8")
    text = release_notes.resolve_notes("v9.9.9", str(tmp_path / "notes"),
                                      str(changelog))
    assert text == ""
    assert release_notes.validate_notes(text)


def test_the_release_notes_script_fails_on_incomplete_notes(tmp_path):
    """脚本本身必须以非零退出码失败 —— 流水线靠它拦下残缺发布。"""
    notes_dir = tmp_path / "release-notes"
    notes_dir.mkdir()
    (notes_dir / "v9.9.9.md").write_text("## 本次更新\n\n### 新增\n- x\n",
                                           encoding="utf-8")
    out = tmp_path / "out.md"
    result = _run_pipeline_script(
        [os.path.join(ROOT, "tools", "release_notes.py"),
         "--tag", "v9.9.9", "--out", str(out),
         "--notes-dir", str(notes_dir),
         "--changelog", str(tmp_path / "nope.md")])
    assert result.returncode != 0, "残缺的小节必须让脚本失败"
    assert "升级提示" in result.stderr


def test_the_release_notes_stderr_survives_a_non_utf8_environment(monkeypatch,
                                                                   tmp_path):
    """WS-532：子进程的中文 stderr 不能被区域编码转义掉。

    Windows runner 的区域编码会退化成 ANSI 代码页，Python 于是把中文写成
    ``\\u5347\\u7ea7\\u63d0\\u793a`` 字面量，「升级提示」再也匹配不上 ——
    门禁本身（退出码 1）没坏，坏的是这条断言。

    这里把环境强制成 ASCII 来复现该条件：跑**真实的**
    ``tools/release_notes.py``，要求 stderr 里仍是可读的中文。
    """
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    notes_dir = tmp_path / "release-notes"
    notes_dir.mkdir()
    (notes_dir / "v9.9.9.md").write_text("## 本次更新\n\n### 新增\n- x\n",
                                           encoding="utf-8")
    result = _run_pipeline_script(
        [os.path.join(ROOT, "tools", "release_notes.py"),
         "--tag", "v9.9.9", "--out", str(tmp_path / "out.md"),
         "--notes-dir", str(notes_dir),
         "--changelog", str(tmp_path / "nope.md")])
    assert result.returncode != 0
    assert "升级提示" in result.stderr, (
        "中文被区域编码转义了：%r" % result.stderr)


def test_the_release_notes_stderr_is_decoded_as_utf8(monkeypatch, tmp_path):
    """父进程必须按 UTF-8 解码子进程的输出，不能依赖 runner 的区域编码。

    子进程写 UTF-8、父进程按 ANSI 代码页解，是同一个坑的另一半：这台
    Linux 的区域编码本来就是 UTF-8，不模拟就测不出来。这里把 locale 的
    首选编码改成 ASCII（Windows 上就是 cp1252/cp936 这类），复现 runner
    的条件。``subprocess`` 在不同 Python 版本上分别问
    ``locale.getencoding()`` 与 ``locale.getpreferredencoding()``，两个都改。
    """
    monkeypatch.setattr(locale, "getencoding", lambda: "ascii", raising=False)
    monkeypatch.setattr(locale, "getpreferredencoding",
                        lambda do_setlocale=True: "ascii", raising=False)
    notes_dir = tmp_path / "release-notes"
    notes_dir.mkdir()
    (notes_dir / "v9.9.9.md").write_text("## 本次更新\n\n### 新增\n- x\n",
                                           encoding="utf-8")
    result = _run_pipeline_script(
        [os.path.join(ROOT, "tools", "release_notes.py"),
         "--tag", "v9.9.9", "--out", str(tmp_path / "out.md"),
         "--notes-dir", str(notes_dir),
         "--changelog", str(tmp_path / "nope.md")])
    assert result.returncode != 0
    assert "升级提示" in result.stderr, (
        "父进程按区域编码解码，中文成了乱码：%r" % result.stderr)


# --- update.json 清单 --------------------------------------------------------

def _fake_assets(tmp_path):
    """Write a fake release asset set and return the directory."""
    (tmp_path / "BindDeckLite.exe").write_bytes(b"fake exe")
    (tmp_path / "ota_binddecklite_esp32_v1.4.2.bin").write_bytes(b"fw esp32")
    (tmp_path / "ota_binddecklite_c3_v1.4.2.bin").write_bytes(b"fw c3")
    (tmp_path / "ota_binddecklite_c3_16mb_v1.4.2.bin").write_bytes(b"fw c3 16mb")
    return tmp_path


def test_the_manifest_hashes_the_real_files(tmp_path):
    """清单里的 sha256/size 必须等于实际文件的。"""
    assets = _fake_assets(tmp_path)
    manifest = build_manifest.build_manifest(str(assets), "1.5.0", "notes", "ts")

    exe = assets / "BindDeckLite.exe"
    assert manifest["app"]["sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert manifest["app"]["size"] == exe.stat().st_size

    fw = assets / "ota_binddecklite_c3_v1.4.2.bin"
    assert manifest["firmware"]["esp32c3"]["sha256"] == \
        hashlib.sha256(fw.read_bytes()).hexdigest()
    assert manifest["firmware"]["esp32c3"]["size"] == fw.stat().st_size


def test_the_manifest_keys_match_the_host_variant_mapping(tmp_path):
    """清单的 firmware key 必须与上位机的型号映射对得上。"""
    assets = _fake_assets(tmp_path)
    manifest = build_manifest.build_manifest(str(assets), "1.5.0")
    assert set(manifest["firmware"]) == {"esp32dev", "esp32c3", "esp32c3_16mb"}
    # 上位机按 chip+flash 选出的 key 必须在清单里。
    sys.path.insert(0, os.path.join(ROOT, "host"))
    import online_update
    for chip, flash in [("ESP32", 4096), ("C3", 4096), ("C3", 16384)]:
        variant = online_update.variant_for_device(chip, flash)
        assert variant in manifest["firmware"]


def test_the_manifest_asset_names_are_the_real_file_names(tmp_path):
    """asset 必须与实际文件名逐字一致 —— 否则下载 404。"""
    assets = _fake_assets(tmp_path)
    manifest = build_manifest.build_manifest(str(assets), "1.5.0")
    for item in list(manifest["firmware"].values()) + [manifest["app"]]:
        assert (assets / item["asset"]).is_file()


def test_the_manifest_fails_when_an_asset_is_missing(tmp_path):
    assets = tmp_path / "partial"
    assets.mkdir()
    (assets / "BindDeckLite.exe").write_bytes(b"exe")
    with pytest.raises(SystemExit):
        build_manifest.build_manifest(str(assets), "1.5.0")


def test_the_manifest_script_writes_valid_json(tmp_path):
    assets = _fake_assets(tmp_path)
    out = tmp_path / "update.json"
    result = _run_pipeline_script(
        [os.path.join(ROOT, "tools", "build_manifest.py"),
         "--assets", str(assets), "--app-version", "1.5.0",
         "--published-at", "2026-11-04T00:00:00Z", "--out", str(out)])
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["app"]["version"] == "1.5.0"
    assert data["published_at"] == "2026-11-04T00:00:00Z"
    assert set(data["firmware"]) == {"esp32dev", "esp32c3", "esp32c3_16mb"}
