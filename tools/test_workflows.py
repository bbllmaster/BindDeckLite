"""Workflow 结构约束测试。

WS-569：workflow 里不允许同一个 job 重复上传同名 artifact。

GitHub Actions 在同一个 workflow run 内对一个 artifact 名只接受一次上传，
第二次会直接报错并让 job 失败：

    Failed to CreateArtifact: Received non-retryable error:
    Failed request: (409) Conflict: an artifact with this name already exists
    on the workflow run

历史事故：build-host.yml 的 windows-exe job 把 `BindDeckLite-windows`
上传了两次，于是任何改动 `main` 的 PR 都会红，和 PR 自己的 diff 无关。

这里直接解析 `.github/workflows/*.yml` 的真实内容（不复制配置），按 job
统计 `actions/upload-artifact` 的 `name`，出现重复即失败。

WS-571：release 按变更路径裁剪构建，测试同时断言

* 路径判定 import 真实生产模块 ``release_changes``（不是抄一份路径清单）；
* ``firmware`` / ``host`` job 的 ``if:`` 带正确的 output 与 tag 兜底；
* ``release`` job 用 ``always()`` 且 needs 三个 job；
* publish 阶段真的从上一版 Release 取回未重建的那一半。

跑法：``python -m pytest tools/test_workflows.py -q``
"""

import glob
import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(ROOT, ".github", "workflows")

#: 测试与生产工具同目录；直接 import 生产模块，禁止在测试里复制判定逻辑。
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import release_changes  # noqa: E402  (sys.path 调整后才能 import)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(ROOT, ".github", "workflows")


def _workflow_paths():
    paths = sorted(glob.glob(os.path.join(WORKFLOW_DIR, "*.yml")) +
                   glob.glob(os.path.join(WORKFLOW_DIR, "*.yaml")))
    assert paths, "no workflow files found under %s" % WORKFLOW_DIR
    return paths


def _load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _artifact_uploads(workflow):
    """Yield (job_id, step_index, artifact_name) for every upload-artifact step."""
    for job_id, job in (workflow.get("jobs") or {}).items():
        steps = (job or {}).get("steps") or []
        for index, step in enumerate(steps):
            uses = str((step or {}).get("uses") or "")
            if "actions/upload-artifact" not in uses:
                continue
            name = ((step or {}).get("with") or {}).get("name")
            yield job_id, index, name


@pytest.mark.parametrize("path", _workflow_paths(), ids=os.path.basename)
def test_no_job_uploads_the_same_artifact_name_twice(path):
    """同一个 job 内 artifact 名必须唯一（否则第二次上传 409）。"""
    seen = {}
    duplicates = []
    for job_id, index, name in _artifact_uploads(_load(path)):
        # 没有字面量名字时才由 GitHub 自动命名，不会撞名。
        if not isinstance(name, str):
            continue
        key = (job_id, name)
        if key in seen:
            duplicates.append(
                "%s: job %r uploads artifact %r at steps %d and %d"
                % (os.path.basename(path), job_id, name, seen[key], index))
        else:
            seen[key] = index

    assert not duplicates, "duplicate upload-artifact name(s):\n" + "\n".join(duplicates)


def test_windows_exe_job_uploads_the_exe_exactly_once():
    """windows-exe 必须产出 `BindDeckLite-windows`，且只有一份。"""
    workflow = _load(os.path.join(WORKFLOW_DIR, "build-host.yml"))
    names = [name for job_id, _index, name in _artifact_uploads(workflow)
             if job_id == "windows-exe"]
    assert names == ["BindDeckLite-windows"]


# ---------------------------------------------------------------------------
# WS-571: release 按变更路径裁剪
# ---------------------------------------------------------------------------

def _release_workflow():
    return _load(os.path.join(WORKFLOW_DIR, "release.yml"))


def test_host_only_change_rebuilds_only_the_exe():
    """只改 host/ → 不重编固件，只重打 exe。"""
    result = release_changes.classify(["host/app.py", "host/static/app.js"])
    assert result["firmware"] is False
    assert result["host"] is True


def test_firmware_only_change_rebuilds_only_the_firmware():
    """只改 src/ → 重编固件，不重打 exe。"""
    result = release_changes.classify(["src/main.cpp"])
    assert result["firmware"] is True
    assert result["host"] is False


def test_firmware_path_set_covers_every_build_input():
    """固件构建输入的每一个目录/文件都必须被识别为固件改动。"""
    for path in ("src/main.cpp", "include/board.h", "lib/Foo/Foo.cpp",
                 "platformio.ini", "partitions/4mb.csv",
                 "bl_idf/platformio.ini", "tools/build_release.py"):
        result = release_changes.classify([path])
        assert result["firmware"] is True, path
        assert result["host"] is False, path


def test_host_path_set_covers_every_packaging_input():
    """上位机打包/清单输入的每一个文件都必须被识别为上位机改动。"""
    for path in ("host/app.py", "tools/build_manifest.py",
                 "tools/release_version.py"):
        result = release_changes.classify([path])
        assert result["host"] is True, path
        assert result["firmware"] is False, path


def test_docs_only_change_builds_nothing():
    """纯文档改动不产生新 Release：两侧都跳过。"""
    result = release_changes.classify(
        ["README.md", "docs/protocol.md", "CHANGELOG.md",
         "release-notes/v1.4.0.md"])
    assert result == {"firmware": False, "host": False, "docs": True}


def test_release_pipeline_files_rebuild_both_halves():
    """发布管线自身改动 → 两侧都重建，产物与流程保持一致。"""
    for path in (".github/workflows/release.yml", "tools/release_notes.py"):
        result = release_changes.classify([path])
        assert result["firmware"] is True, path
        assert result["host"] is True, path


def test_unknown_path_rebuilds_both_halves():
    """认不出来的路径保守全量重建，绝不少发。"""
    result = release_changes.classify(["some-new-dir/tool.py"])
    assert result["firmware"] is True
    assert result["host"] is True


def test_empty_diff_rebuilds_both_halves():
    result = release_changes.classify([])
    assert result["firmware"] is True
    assert result["host"] is True


def test_release_tag_forces_both_halves():
    """正式 tag 必须全量重建（tag 可复现），不沿用上一版产物。"""
    result = release_changes.decide("refs/tags/v1.4.0", before="deadbeef",
                                   sha="cafebabe")
    assert result["firmware"] is True
    assert result["host"] is True


def test_changes_job_feeds_the_outputs_the_other_jobs_read():
    """changes job 产出 firmware/host/docs，且真的调用生产判定脚本。"""
    workflow = _release_workflow()
    changes = workflow["jobs"]["changes"]
    assert set(changes["outputs"]) == {"firmware", "host", "docs"}

    steps = changes["steps"]
    scripts = "\n".join(str(step.get("run") or "") for step in steps)
    assert "tools/release_changes.py" in scripts, (
        "changes job 必须调用 tools/release_changes.py（路径集合的唯一来源）")
    # 判定结果要写进 $GITHUB_OUTPUT，否则下游 if: 永远读到空值。
    assert "GITHUB_OUTPUT" in scripts


def test_firmware_and_host_jobs_are_gated_on_their_change_flag():
    """两个构建 job 各自按 changes 的 output 裁剪，并保留 tag 兜底。"""
    jobs = _release_workflow()["jobs"]
    for job_id, flag in (("firmware", "firmware"), ("host", "host")):
        job = jobs[job_id]
        assert "changes" in str(job.get("needs") or ""), \
            "%s job 必须 needs changes 才能读到判定结果" % job_id
        condition = str(job.get("if") or "")
        assert "needs.changes.outputs.%s == 'true'" % flag in condition, (
            "%s job 的 if: 必须按 %s 改动裁剪，实际为 %r"
            % (job_id, flag, condition))
        assert "refs/tags/v" in condition, (
            "%s job 的 if: 必须对正式 tag 强制重建，实际为 %r"
            % (job_id, condition))


def test_release_job_runs_when_one_side_is_skipped():
    """裁剪后 release 仍要跑：always() + needs 三个 job + 至少一侧有改动。"""
    release = _release_workflow()["jobs"]["release"]
    needs = release.get("needs")
    assert sorted(needs) == ["changes", "firmware", "host"], needs

    condition = " ".join(str(release.get("if") or "").split())
    assert "always()" in condition
    assert "!failure()" in condition
    assert "!cancelled()" in condition
    # 纯文档改动不发布：必须要求 firmware 或 host 至少为 true。
    assert "needs.changes.outputs.firmware == 'true'" in condition
    assert "needs.changes.outputs.host == 'true'" in condition
    assert "refs/tags/v" in condition


def test_release_job_reuses_the_other_half_from_the_previous_release():
    """被跳过的半边要从上一版 Release 拷贝资产，而不是留空。"""
    steps = _release_workflow()["jobs"]["release"]["steps"]
    reuse = [step for step in steps
             if "previous release" in str(step.get("name") or "")]
    assert reuse, "release job 缺少沿用上一版产物的步骤"

    script = "\n".join(str(step.get("run") or "") for step in reuse)
    assert "gh release download" in script
    assert "--pattern '*.bin'" in script
    assert "BindDeckLite.exe" in script
    # 沿用的 exe 版本号来自上一版 update.json。
    assert "update.json" in script

    # 两个 half 的 artifact 必须各自按名字下载：按名字下载不存在的 artifact
    # 会直接失败，所以不能共用一次无条件的 download-artifact。
    named = {str((step.get("with") or {}).get("name")): str(step.get("if") or "")
             for step in steps
             if "download-artifact" in str(step.get("uses") or "")}
    assert named.get("release-firmware", "").find("needs.firmware.result") >= 0
    assert named.get("release-exe", "").find("needs.host.result") >= 0
