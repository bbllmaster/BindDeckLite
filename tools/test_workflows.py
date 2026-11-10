"""WS-569: workflow 里不允许同一个 job 重复上传同名 artifact。

GitHub Actions 在同一个 workflow run 内对一个 artifact 名只接受一次上传，
第二次会直接报错并让 job 失败：

    Failed to CreateArtifact: Received non-retryable error:
    Failed request: (409) Conflict: an artifact with this name already exists
    on the workflow run

历史事故：build-host.yml 的 windows-exe job 把 `BindDeckLite-windows`
上传了两次，于是任何改动 `main` 的 PR 都会红，和 PR 自己的 diff 无关。

这里直接解析 `.github/workflows/*.yml` 的真实内容（不复制配置），按 job
统计 `actions/upload-artifact` 的 `name`，出现重复即失败。

跑法：``python -m pytest tools/test_workflows.py -q``
"""

import glob
import os

import pytest
import yaml

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
