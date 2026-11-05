#!/usr/bin/env python3
"""BindDeckLite host i18n guard (WS-529 / WS-534).

Two checks, both required before a host change can merge:

1. **Key alignment** — ``host/locales/zh-CN.json`` (the single source of truth)
   and ``host/locales/en-US.json`` must have exactly the same key set, and a
   key must carry the same ``{placeholder}`` names in both files.  The keys are
   read straight from the locale files; this script never keeps its own copy of
   the list, so adding a string to a page and forgetting the translation fails
   here instead of shipping half-translated UI.

2. **Hard-coded Chinese** — user-visible Chinese left in ``host/**.py`` /
   ``**.js`` / ``**.html``.  Chinese *comments* are fine (the repo is written in
   Chinese); only string literals, HTML text nodes and HTML attribute values are
   scanned.  Python is parsed with ``tokenize`` + ``ast`` so docstrings and
   comments are skipped, JS comments (``//`` and ``/* */``) are stripped, HTML
   comments, ``<script>`` and ``<style>`` bodies are skipped.

A finding can be waived through ``tools/i18n_whitelist.txt``.  Each line is
``<path-glob>\t<reason>[\t<text-regex>]``; the reason is mandatory because a
whitelist entry without a reason is a rubber stamp.  Only protocol literals
(``CFG:`` / ``CMD:`` / ``BTN:`` / ``ENC:`` / ``VERSION:``), the language's own
name ("中文"), test fixtures and raw device lines belong there — real UI copy
does not.

Exit code: 0 clean, 1 violations.
"""

from __future__ import annotations

import ast
import io
import json
import re
import sys
import tokenize
from fnmatch import fnmatch
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "host"
LOCALES = HOST / "locales"
WHITELIST = Path(__file__).resolve().parent / "i18n_whitelist.txt"

ZH = re.compile(r"[\u4e00-\u9fff]")
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Test code is not user-visible UI: its Chinese lives in assertion messages and
# test titles.  Scanning it would force a whitelist entry per test case, which
# would bury the entries that actually matter.
SKIP_PATTERNS = (
    "test_*",
    "conftest.py",
    "*.spec",
    "*_test.py",
)


def _skipped(path: Path) -> bool:
    return any(fnmatch(path.name, pat) for pat in SKIP_PATTERNS)


# --------------------------------------------------------------------------
# locales
# --------------------------------------------------------------------------
def load_locales() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for tag in ("zh-CN", "en-US"):
        path = LOCALES / f"{tag}.json"
        if not path.exists():
            raise SystemExit(f"missing locale file: {path.relative_to(ROOT)}")
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise SystemExit(f"{path.relative_to(ROOT)} must be a flat JSON object")
        bad = [k for k, v in data.items() if not isinstance(v, str)]
        if bad:
            raise SystemExit(f"{path.relative_to(ROOT)}: non-string values: {bad[:5]}")
        out[tag] = data
    return out


def check_locales(locales: dict[str, dict[str, str]]) -> list[str]:
    errors: list[str] = []
    zh, en = locales["zh-CN"], locales["en-US"]
    for missing in sorted(set(zh) - set(en)):
        errors.append(f"en-US.json: missing key {missing!r} (present in zh-CN.json)")
    for extra in sorted(set(en) - set(zh)):
        errors.append(f"en-US.json: extra key {extra!r} (not in zh-CN.json)")
    for key in sorted(set(zh) & set(en)):
        a = set(PLACEHOLDER.findall(zh[key]))
        b = set(PLACEHOLDER.findall(en[key]))
        if a != b:
            errors.append(
                f"{key}: placeholder mismatch (zh={sorted(a)} en={sorted(b)})"
            )
    return errors


# --------------------------------------------------------------------------
# whitelist
# --------------------------------------------------------------------------
def load_whitelist() -> list[tuple[str, str, re.Pattern[str] | None]]:
    entries: list[tuple[str, str, re.Pattern[str] | None]] = []
    if not WHITELIST.exists():
        return entries
    for lineno, raw in enumerate(WHITELIST.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 2 or not parts[1].strip():
            raise SystemExit(
                f"tools/i18n_whitelist.txt:{lineno}: expected <glob>\\t<reason>[\\t<regex>]"
            )
        pattern = re.compile(parts[2]) if len(parts) > 2 and parts[2].strip() else None
        entries.append((parts[0].strip(), parts[1].strip(), pattern))
    return entries


def whitelisted(
    rel: str, text: str, entries: list[tuple[str, str, re.Pattern[str] | None]]
) -> bool:
    for glob, _reason, pattern in entries:
        if not fnmatch(rel, glob):
            continue
        if pattern is None or pattern.search(text):
            return True
    return False


# --------------------------------------------------------------------------
# scanners — each returns [(lineno, text)]
# --------------------------------------------------------------------------
def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def scan_python(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:  # a broken file is a finding in itself
        return [(exc.lineno or 0, f"cannot parse: {exc.msg}")]
    docstrings = _docstring_ids(tree)
    findings: list[tuple[int, str]] = []
    # The constant chunks of an f-string are reported together with the whole
    # f-string; reporting them again would list every fragment twice.
    fstring_parts = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }

    # ast gives the literal's real value (f-strings included) and its line; the
    # tokenizer would hand us raw quotes and joined fragments.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or id(node) in fstring_parts:
                continue
            if ZH.search(node.value):
                findings.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                str(v.value) for v in node.values if isinstance(v, ast.Constant)
            )
            if ZH.search(text):
                findings.append((node.lineno, text))
    return findings


def _strip_js(src: str) -> tuple[str, list[int]]:
    """Blank out JS comments, keeping line numbers intact."""
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        ch = src[i]
        if quote:
            if ch == "\\":
                out.append(src[i : i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(ch)
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out), []


JS_STRING = re.compile(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`", re.S)


def scan_js(path: Path) -> list[tuple[int, str]]:
    src, _ = _strip_js(path.read_text(encoding="utf-8"))
    findings: list[tuple[int, str]] = []
    for match in JS_STRING.finditer(src):
        literal = match.group(0)
        if not ZH.search(literal):
            continue
        # data-i18n / data-i18n-* keys are ASCII by contract; a Chinese one
        # means somebody put copy in the attribute instead of the table.
        line = src.count("\n", 0, match.start()) + 1
        findings.append((line, literal))
    return findings


class _TextCollector(HTMLParser):
    """Collect Chinese text nodes / attribute values that are *not* translated.

    A text node under an element with ``data-i18n`` (or ``data-i18n-html``) is
    already fed from the locale table, so it is not a finding — that is what
    lets the template keep the Chinese as first-paint fallback.  The same goes
    for ``title`` / ``placeholder`` / ``aria-label`` values whose element
    carries the matching ``data-i18n-*`` attribute.
    """

    ATTR_KEYS = {
        "title": "data-i18n-title",
        "placeholder": "data-i18n-placeholder",
        "aria-label": "data-i18n-aria-label",
        "alt": "data-i18n-alt",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.findings: list[tuple[int, str]] = []
        self._stack: list[tuple[str, bool]] = []
        self._skip = 0

    @property
    def _translated(self) -> bool:
        return any(flag for _tag, flag in self._stack)

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        mapping = dict(attrs)
        translated = "data-i18n" in mapping or "data-i18n-html" in mapping
        self._stack.append((tag, translated))
        if self._skip:
            return
        line = self.getpos()[0]
        for attr, key in self.ATTR_KEYS.items():
            value = mapping.get(attr) or ""
            if ZH.search(value) and not mapping.get(key):
                self.findings.append((line, value))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self._stack:
            self._stack.pop()

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._skip or self._translated:
            return
        if ZH.search(data):
            self.findings.append((self.getpos()[0], data.strip()))


def scan_html(path: Path) -> list[tuple[int, str]]:
    collector = _TextCollector()
    collector.feed(path.read_text(encoding="utf-8"))
    collector.close()
    return [(line, text) for line, text in collector.findings if text]


SCANNERS = {".py": scan_python, ".js": scan_js, ".html": scan_html}


def scan_host() -> tuple[list[str], int]:
    violations: list[str] = []
    scanned = 0
    whitelist = load_whitelist()
    for path in sorted(HOST.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNERS:
            continue
        if any(part in {"dist", "build", "__pycache__", "node_modules", ".pytest_cache"} for part in path.parts):
            continue
        if _skipped(path):
            continue
        rel = path.relative_to(ROOT).as_posix()
        scanned += 1
        for lineno, text in SCANNERS[path.suffix](path):
            snippet = re.sub(r"\s+", " ", text).strip()
            if whitelisted(rel, snippet, whitelist):
                continue
            violations.append(f"{rel}:{lineno}: {snippet}")
    return violations, scanned


def main() -> int:
    errors: list[str] = []
    try:
        locales = load_locales()
    except SystemExit as exc:
        errors.append(str(exc))
    else:
        errors.extend(check_locales(locales))

    violations, scanned = scan_host()

    if errors:
        print("locale check failed:")
        for err in errors:
            print(f"  {err}")
    if violations:
        print(f"hard-coded Chinese in {len(violations)} place(s):")
        for item in violations:
            print(f"  {item}")
    if errors or violations:
        print()
        print(f"FAILED — {len(errors)} locale problem(s), {len(violations)} hard-coded string(s)")
        return 1

    print(
        f"OK — locale keys aligned ({len(locales['zh-CN'])} keys), "
        f"no hard-coded Chinese in {scanned} scanned file(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
