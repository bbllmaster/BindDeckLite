"""WS-570: Windows 真机回归 —— 真的执行生成的 ``update.bat``。

这个 bug 只在 Windows 上复现：下载完成后 CMD 一闪而过、更新失败、没有日志。
单测只能断言脚本「长什么样」，所以这里用 ``cmd /c`` 真跑一遍生成的脚本。

这里**不复制生产逻辑**：脚本由 ``online_update.build_update_script`` 生成、
``online_update.stage_update_script`` 按生产编码落盘，断言也复用
``online_update.read_update_log`` 解析出的日志。

注意：脚本会 ``start`` 子进程，所以**不能用 ``capture_output``** —— ``start``
拉起的进程会继承管道句柄，``communicate()`` 会等到管道 EOF，一旦子进程不退出
就永远不返回（PR #72 的 windows-exe 就是这样挂住的）。标准流一律给 NUL，
超时直接 ``taskkill /T``。

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
WAIT_SECONDS = 6
PID_WAIT_SECONDS = 4
#: 一个几乎不可能存在的 PID：tasklist 查不到它，脚本立刻进入 copy 阶段。
DEAD_PID = 999999
#: 脚本自身最长 ~13 秒（PID 等待 + marker 等待），留足余量后强杀。
RUN_TIMEOUT = 60


def _stub(path: Path, tag: str, host_marker: Path) -> None:
    """假「上位机 exe」：启动后写 started-<tag>.txt 与 host marker 就退出。

    用 ``.bat`` 当目标 exe：``start`` 能像 exe 一样把它拉起来，复制 / 备份 /
    启动逻辑与真实 exe 完全一致。
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
    """exe 所在目录 + updates\\<version>（与生产布局一致）。"""
    root = tmp_path / name
    updates = root / "updates" / "1.5.0"
    updates.mkdir(parents=True)
    return root, updates


def _prepare(updates: Path, exe: Path, new: Path, marker: Path, log: Path) -> Path:
    script = online_update.build_update_script(
        exe_path=str(exe),
        new_exe=str(new),
        backup_exe=str(exe) + ".bak",
        marker_path=str(marker),
        pid=DEAD_PID,
        wait_seconds=WAIT_SECONDS,
        log_path=str(log),
        pid_wait_seconds=PID_WAIT_SECONDS,
    )
    path = updates / "update.bat"
    online_update.stage_update_script(str(path), script, str(marker))
    return path


def _run(script: Path, workdir: Path) -> None:
    """跑一遍脚本；不接管道（``start`` 的子进程会继承句柄、拖住 communicate）。"""
    proc = subprocess.Popen(["cmd", "/c", str(script)], cwd=str(workdir),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        code = proc.wait(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        pytest.fail("update.bat 超过 %d 秒未结束" % RUN_TIMEOUT)
    assert code == 0, "update.bat 退出码 %s" % code


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

    _run(_prepare(updates, exe, new, marker, log), root)

    assert exe.read_bytes() == new_bytes, "新 exe 必须被复制到目标位置"
    assert backup.read_bytes() == old_bytes, "旧 exe 必须先备份为 .bak"
    assert _wait_for(root / "started-new.txt"), "新版本必须被启动"

    info = online_update.read_update_log(str(log))
    lines = "\n".join(info["lines"])
    assert info["result"] == "ok", lines
    assert "[waitpid]" in lines
    assert "step=backup result=ok" in lines
    assert "step=replace result=ok" in lines
    assert "[start] new version launched" in lines


def test_script_reports_a_missing_new_exe_and_keeps_the_old_one(tmp_path):
    """NEW 缺失：走 ``:fail``，EXE 不被破坏，旧版本被重新拉起，日志有记录。"""
    root, updates = _case(tmp_path, "missing")
    exe = root / "BindDeckLite.bat"
    new = updates / "BindDeckLite.bat"       # 故意不创建
    marker = updates / "updated.marker"
    log = updates.parent / "update.log"

    _stub(exe, "old", marker)
    old_bytes = exe.read_bytes()

    _run(_prepare(updates, exe, new, marker, log), root)

    assert exe.read_bytes() == old_bytes, "失败时绝不能动原 exe"
    assert not Path(str(exe) + ".bak").exists(), "没到替换阶段不该产生备份"
    assert _wait_for(root / "started-old.txt"), "旧版本必须被重新拉起"

    info = online_update.read_update_log(str(log))
    assert info["result"] == "fail", info["lines"]
    assert info["reason"] == "new_missing"
