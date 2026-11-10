"""WS-570: Windows 真机回归 —— 真的执行生成的 ``update.bat``。

这个 bug 只在 Windows 上复现：下载完成后 CMD 一闪而过、更新失败、没有任何
日志。单测只能断言脚本「长什么样」，唯一可靠的回归是真跑一遍脚本。

这里**不复制生产逻辑**：脚本由 ``online_update.build_update_script`` 生成、
由 ``online_update.stage_update_script`` 按生产编码落盘，然后用 ``cmd /c``
执行；断言也复用 ``online_update.read_update_log`` 解析出来的日志。

覆盖（对应 issue 的验收项）：

* 正常路径：NEW 被复制到 EXE、BAK 生成、新版本被启动、日志有每一步；
* ``NEW`` 缺失：走 ``:fail``，EXE 不被破坏，旧版本被重新拉起，日志有记录；
* 目标 exe 只读：``attrib -R`` 后仍能替换；
* 日志包含 ``[waitpid]`` / ``[copy]`` / ``[result]`` 各步骤。

非 Windows 上整体跳过：批处理只有 cmd.exe 能执行。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import online_update  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="update.bat 只有 cmd.exe 能执行")

#: 测试里把两个等待都缩短：脚本逻辑不变，只是别让 CI 等满 60 + 120 秒。
WAIT_SECONDS = 8
PID_WAIT_SECONDS = 5
#: 一个几乎不可能存在的 PID：tasklist 查不到它，脚本立刻进入 copy 阶段。
DEAD_PID = 999999


def _stub(path: Path, tag: str, host_marker: Path) -> None:
    """假「上位机 exe」：启动后写 started-<tag>.txt 与 host marker 就退出。

    用 ``.bat`` 当目标 exe：``start`` 能像 exe 一样把它拉起来，而真正的
    config 路径 / 复制 / 启动逻辑与真实 exe 完全一致。
    """
    text = "\r\n".join([
        "@echo off",
        'echo %s>"%%~dp0started-%s.txt"' % (tag, tag),
        'echo started>"%s"' % host_marker,
        "exit /b 0",
        "",
    ])
    online_update.write_update_script(str(path), text)


def _case(tmp_path: Path, name: str):
    """建一套目录：exe 所在目录 + updates\\<version>（与生产布局一致）。"""
    root = tmp_path / name
    updates = root / "updates" / "1.5.0"
    updates.mkdir(parents=True)
    return root, updates


def _prepare(updates: Path, exe: Path, new: Path, marker: Path, log: Path) -> Path:
    """用生产函数生成并落盘 update.bat（含清陈旧 marker 的 stage 步骤）。"""
    script = online_update.build_update_script(
        exe_path=str(exe),
        new_exe=str(new),
        backup_exe=str(exe) + ".bak",
        marker_path=str(marker),
        pid=DEAD_PID,
        wait_seconds=WAIT_SECONDS,
        log_path=str(log),
        copy_retry_seconds=online_update.UPDATE_COPY_RETRY_SECONDS,
        pid_wait_seconds=PID_WAIT_SECONDS,
    )
    path = updates / "update.bat"
    online_update.stage_update_script(str(path), script, str(marker))
    return path


def _run(script: Path, workdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["cmd", "/c", str(script)], cwd=str(workdir),
                          capture_output=True, text=True, errors="replace",
                          timeout=300)


def _wait_for(path: Path, timeout: float = 20.0) -> bool:
    """``start`` 是异步的：轮询等新进程写下自己的标记。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return path.exists()


def test_script_swaps_the_exe_and_starts_the_new_version(tmp_path):
    """正常路径：NEW → EXE、EXE → BAK、新版本被启动、日志逐步骤记录。"""
    root, updates = _case(tmp_path, "normal")
    exe = root / "BindDeckLite.bat"
    new = updates / "BindDeckLite.bat"
    backup = Path(str(exe) + ".bak")
    marker = updates / "updated.marker"
    log = updates.parent / "update.log"

    _stub(exe, "old", marker)
    old_bytes = exe.read_bytes()
    _stub(new, "new", marker)
    new_bytes = new.read_bytes()

    result = _run(_prepare(updates, exe, new, marker, log), root)

    assert result.returncode == 0, result.stderr
    assert exe.read_bytes() == new_bytes, "新 exe 必须被复制到目标位置"
    assert backup.read_bytes() == old_bytes, "旧 exe 必须先备份为 .bak"
    assert _wait_for(root / "started-new.txt"), "新版本必须被启动"

    info = online_update.read_update_log(str(log))
    assert info["result"] == "ok", info["lines"]
    lines = "\n".join(info["lines"])
    assert "[waitpid]" in lines
    assert "step=backup result=ok" in lines
    assert "step=replace result=ok" in lines
    assert "[start] new version launched" in lines
    assert "[done]" in lines


def test_script_reports_a_missing_new_exe_and_keeps_the_old_one(tmp_path):
    """NEW 缺失：走 ``:fail``，EXE 不被破坏，旧版本被重新拉起，日志有记录。"""
    root, updates = _case(tmp_path, "missing")
    exe = root / "BindDeckLite.bat"
    new = updates / "BindDeckLite.bat"       # 故意不创建
    marker = updates / "updated.marker"
    log = updates.parent / "update.log"

    _stub(exe, "old", marker)
    old_bytes = exe.read_bytes()

    result = _run(_prepare(updates, exe, new, marker, log), root)

    assert result.returncode == 0, result.stderr
    assert exe.read_bytes() == old_bytes, "失败时绝不能动原 exe"
    assert not Path(str(exe) + ".bak").exists(), "没到替换阶段不该产生备份"
    assert _wait_for(root / "started-old.txt"), "旧版本必须被重新拉起"

    info = online_update.read_update_log(str(log))
    assert info["result"] == "fail", info["lines"]
    assert info["reason"] == "new_missing"
    assert "new_missing" in info["detail"]


def test_read_only_target_exe_is_replaced_after_attrib_r(tmp_path):
    """目标 exe 只读：脚本先 ``attrib -R`` 再 copy，仍然能替换成功。"""
    root, updates = _case(tmp_path, "readonly")
    exe = root / "BindDeckLite.bat"
    new = updates / "BindDeckLite.bat"
    marker = updates / "updated.marker"
    log = updates.parent / "update.log"

    _stub(exe, "old", marker)
    _stub(new, "new", marker)
    new_bytes = new.read_bytes()
    subprocess.run(["attrib", "+R", str(exe)], check=True, capture_output=True)

    result = _run(_prepare(updates, exe, new, marker, log), root)

    assert result.returncode == 0, result.stderr
    assert exe.read_bytes() == new_bytes, "只读属性必须先被清掉才能替换"
    assert _wait_for(root / "started-new.txt")
    assert online_update.read_update_log(str(log))["result"] == "ok"


def test_every_step_lands_in_the_log_even_before_the_copy(tmp_path):
    """日志从第一步就开始写：失败发生在 copy 之前也要能定位。"""
    root, updates = _case(tmp_path, "log")
    exe = root / "BindDeckLite.bat"
    new = updates / "BindDeckLite.bat"
    marker = updates / "updated.marker"
    log = updates.parent / "update.log"

    _stub(exe, "old", marker)
    _stub(new, "new", marker)
    # 上一次失败的日志必须被本次运行覆盖，不能误导诊断。
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("[result] rollback reason=copy_failed\r\n", encoding="cp936")

    _run(_prepare(updates, exe, new, marker, log), root)

    info = online_update.read_update_log(str(log))
    assert "[start]" in "\n".join(info["lines"]), "本次运行的 [start] 必须存在"
    assert info["result"] == "ok", "上一次的失败结论不能留下来解释这一次"
