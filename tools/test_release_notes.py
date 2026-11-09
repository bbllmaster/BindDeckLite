"""WS-558: rolling Release 的「本次更新」只列自上一个 rolling 以来的增量。

每个 nightly 都重复全部历史、越滚越长的根因是 `release_notes.changelog_summary()`
把「自上一个正式版以来」的所有条目当成「本次更新」。这里用**临时真实 git 仓库**
造出 main-A → main-B 两次 rolling，断言说明只含 A..B 的提交，且 commit 类型
分别落到「新增 / 修复 / 变更」。

测试 import 的是 `tools/release_notes.py` 的生产函数，不是复制一份逻辑。

跑法：``pytest tools/test_release_notes.py -q``
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import release_notes  # noqa: E402

RELEASE_YML = os.path.join(ROOT, ".github", "workflows", "release.yml")
SCRIPT = os.path.join(ROOT, "tools", "release_notes.py")


def _git(repo, *args):
    """Run git inside *repo* with a throwaway identity (never the runner's)."""
    return subprocess.run(
        ["git", "-c", "user.name=Release Notes Test",
         "-c", "user.email=notes@example.invalid",
         "-c", "commit.gpgsign=false",
         "-c", "core.autocrlf=false"] + list(args),
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
        check=True)


def _new_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _commit(repo, message):
    """Append to a file so every commit carries a real change."""
    path = repo / "history.txt"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(message + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


# --- 增量范围 ----------------------------------------------------------------

def test_rolling_notes_only_cover_the_previous_rolling(tmp_path):
    """验收 1：main-B 的说明只含 main-A..B，不重复 A 之前的内容。"""
    repo = _new_repo(tmp_path)
    _commit(repo, "feat: 第一版功能")
    _git(repo, "tag", "main-20260101-0000")
    _commit(repo, "feat: 第二版功能")
    _commit(repo, "fix: 修好夜间崩溃")

    notes = release_notes.rolling_notes("main-20260101-0000", repo=str(repo))

    assert "第二版功能" in notes
    assert "修好夜间崩溃" in notes
    assert "第一版功能" not in notes, "上一个 rolling 之前的提交不该再出现"
    assert release_notes.validate_notes(notes) == []


def test_the_previous_rolling_tag_wins_over_the_formal_tag(tmp_path):
    """有 main-* 时不再拿正式版当起点，否则 nightly 仍会重复正式版之后的内容。"""
    repo = _new_repo(tmp_path)
    _commit(repo, "feat: 正式版功能")
    _git(repo, "tag", "v1.4.0")
    _commit(repo, "feat: 第一次 rolling 带的改动")
    _git(repo, "tag", "main-20260101-0000")
    _commit(repo, "feat: 第二次 rolling 带的改动")

    assert release_notes.resolve_since(repo=str(repo)) == "main-20260101-0000"


def test_the_first_rolling_starts_at_the_last_formal_tag(tmp_path):
    """验收 2：第一次 rolling（之前只有 v1.4.0）→ 说明是 v1.4.0..HEAD。"""
    repo = _new_repo(tmp_path)
    _commit(repo, "feat: 正式版里的功能")
    _git(repo, "tag", "v1.4.0")
    _commit(repo, "fix: 正式版之后的修复")

    assert release_notes.resolve_since(repo=str(repo)) == "v1.4.0"
    notes = release_notes.rolling_notes(repo=str(repo))
    assert "正式版之后的修复" in notes
    assert "正式版里的功能" not in notes


def test_the_first_rolling_without_any_tag_lists_the_first_commit(tmp_path):
    """一个 tag 都没有时从第一个提交开始 —— 不能因为取不到起点就发空说明。"""
    repo = _new_repo(tmp_path)
    _commit(repo, "feat: 唯一的功能")

    assert release_notes.resolve_since(repo=str(repo)) == ""
    notes = release_notes.rolling_notes(repo=str(repo))
    assert "唯一的功能" in notes
    assert release_notes.validate_notes(notes) == []


def test_an_empty_range_still_produces_a_valid_body(tmp_path):
    """两次 rolling 之间没有提交 → 结构完整、每个小节「无」，而不是失败。"""
    repo = _new_repo(tmp_path)
    _commit(repo, "feat: 已发布的功能")
    _git(repo, "tag", "main-20260101-0000")

    notes = release_notes.rolling_notes("main-20260101-0000", repo=str(repo))
    assert release_notes.validate_notes(notes) == []
    assert "已发布的功能" not in notes


# --- 按提交类型归类 ----------------------------------------------------------

@pytest.mark.parametrize("subject, body, section", [
    ("feat: 新面板", "", "新增"),
    ("feat(ui): 新面板", "", "新增"),
    ("fix: 修好崩溃", "", "修复"),
    ("fix(ble): 断线重连", "", "修复"),
    ("refactor: 拆函数", "", None),
    ("docs: 补协议说明", "", None),
    ("chore: 升依赖", "", None),
    ("test: 加用例", "", None),
    ("feat!: 重做协议", "", "变更"),
    ("feat: 重做协议", "BREAKING CHANGE: 旧固件不再兼容", "变更"),
    ("WS-546: 每个输入可配修饰键", "", "新增"),
])
def test_commit_types_land_in_the_right_section(subject, body, section):
    """验收 3：fix: 必须进「修复」；breaking 进「变更」；无前缀的仓库风格不丢。"""
    entry = release_notes.classify_commit(subject, body)
    if section is None:
        assert entry is None
    else:
        assert entry is not None
        assert entry[0] == section


def test_a_fix_is_not_written_as_wu_anymore(tmp_path):
    """验收 3：有 fix 时「### 修复」不能再是无条件「- 无」。"""
    repo = _new_repo(tmp_path)
    _commit(repo, "feat: 起点")
    _git(repo, "tag", "main-20260101-0000")
    _commit(repo, "fix: 修好夜间崩溃")

    notes = release_notes.rolling_notes("main-20260101-0000", repo=str(repo))
    fixed = notes.split("### 修复", 1)[1].split("###", 1)[0]
    assert "修好夜间崩溃" in fixed
    assert "- 无" not in fixed


def test_duplicate_subjects_are_listed_once():
    """同一提交被 merge/cherry-pick 重复带进来时只写一条。"""
    notes = release_notes.summarize_commits([
        ("feat: 新面板", ""),
        ("feat: 新面板", ""),
        ("fix: 修好崩溃", ""),
        ("fix: 修好崩溃", ""),
    ])
    assert notes.count("新面板") == 1
    assert notes.count("修好崩溃") == 1


def test_breaking_changes_are_called_out_in_the_upgrade_hint():
    notes = release_notes.summarize_commits([("feat!: 重做协议", "")])
    assert "重做协议" in notes.split("### 变更 / 不兼容", 1)[1]
    assert "- 本次包含不兼容变更" in notes


# --- 正式版行为不变 ----------------------------------------------------------

def test_formal_tags_still_use_the_changelog_section(tmp_path):
    """验收 4：v* 仍取 CHANGELOG 的 `## <tag>` 段，不读 git 范围。"""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## v1.5.0 — 2026-11-09\n\n### 固件\n\n- 正式版内容\n\n"
        "## v1.4.0 — 2026-10-19\n\n- 旧内容\n", encoding="utf-8")

    text = release_notes.resolve_notes("v1.5.0", "", str(changelog))
    assert "正式版内容" in text
    assert "旧内容" not in text


def test_a_per_tag_notes_file_still_wins(tmp_path):
    notes_dir = tmp_path / "release-notes"
    notes_dir.mkdir()
    (notes_dir / "v1.5.0.md").write_text("## 本次更新\n\n### 新增\n- 手写\n",
                                          encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## v1.5.0 — 2026-11-09\n\n- CHANGELOG\n", encoding="utf-8")

    assert "手写" in release_notes.resolve_notes("v1.5.0", str(notes_dir),
                                                  str(changelog))


# --- 命令行入口 --------------------------------------------------------------

def test_the_cli_writes_only_the_increment(tmp_path):
    """回归入口：``--since`` 之前根本不存在（argparse 退出 2），现在写增量。"""
    repo = _new_repo(tmp_path)
    _commit(repo, "feat: 第一版功能")
    _git(repo, "tag", "main-20260101-0000")
    _commit(repo, "fix: 修好夜间崩溃")

    out = tmp_path / "NOTES.md"
    result = subprocess.run(
        [sys.executable, SCRIPT,
         "--tag", "main-20260102-0000", "--out", str(out),
         "--notes-dir", str(tmp_path / "no-notes"),
         "--changelog", str(tmp_path / "no-changelog.md"),
         "--repo", str(repo),
         "--since", "main-20260101-0000"],
        capture_output=True, text=True, encoding="utf-8")

    assert result.returncode == 0, result.stderr
    text = out.read_text(encoding="utf-8")
    assert "修好夜间崩溃" in text
    assert "第一版功能" not in text
    assert release_notes.validate_notes(text) == []
    # fix: 落在「### 修复」与「### 变更」之间。
    assert text.index("### 修复") < text.index("修好夜间崩溃") < text.index("### 变更")


def test_the_cli_still_fails_on_incomplete_formal_notes(tmp_path):
    """验收 4 的硬门槛不变：正式 tag 缺小节 → 非零退出，发布 job 失败。"""
    out = tmp_path / "NOTES.md"
    result = subprocess.run(
        [sys.executable, SCRIPT, "--tag", "v9.9.9", "--out", str(out),
         "--notes-dir", str(tmp_path / "no-notes"),
         "--changelog", str(tmp_path / "no-changelog.md")],
        capture_output=True, text=True, encoding="utf-8")

    assert result.returncode != 0
    assert "升级提示" in result.stderr


# --- 流水线接线 --------------------------------------------------------------

def test_the_release_workflow_passes_since_and_fetches_history():
    """release.yml 必须把上一个 main-* tag 传给脚本，并完整拉取历史与 tag。"""
    with open(RELEASE_YML, "r", encoding="utf-8") as handle:
        text = handle.read()

    assert "--since" in text
    assert "--tag" in text
    # 只要有一处 checkout 没拉全历史，git log <since>..HEAD 就会失败或为空。
    assert "fetch-depth: 0" in text
    assert "fetch-tags: true" in text
    assert "main-*" in text
