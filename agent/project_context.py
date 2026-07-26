"""扫描当前工作目录，拼一段短小的「项目上下文」塞进 system prompt。

目标：让模型一上来就知道项目类型、可用脚本、git 状态和仓库规则，
少做几轮盲目 list_dir / 猜命令。内容严格截断，避免撑爆上下文。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

# 整段项目上下文上限（字符）；规则文件各自也有上限
_MAX_CONTEXT_CHARS = 7_000
_MAX_RULES_CHARS = 2_500
_MAX_README_CHARS = 1_200
_MAX_SCRIPTS = 24

# 按优先级尝试的规则/说明文件（找到就读，都可读可叠加）
_RULE_FILES = (
    "AGENTS.md",
    "AGENT.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
    "CLAUDE.md",
)

_README_CANDIDATES = ("README.md", "README.zh-CN.md", "README.zh.md", "readme.md")

# 用于识别「这是个什么项目」的标记文件
_PROJECT_MARKERS = (
    "package.json",
    "pnpm-workspace.yaml",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "Gemfile",
    "CMakeLists.txt",
)


def _read_text(path: Path, limit: int) -> str:
    """读文本并截断；失败返回空串。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.replace("\r\n", "\n").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…[truncated]"
    return text


def _git_snapshot(cwd: Path) -> str:
    """短 git 摘要：分支 + status --short（限行）。"""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if branch.returncode != 0:
            return ""
        name = (branch.stdout or "").strip() or "(unknown)"

        status = subprocess.run(
            ["git", "status", "--short", "-b"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        lines = [ln for ln in (status.stdout or "").splitlines() if ln.strip()]
        # 第一行常是 ## branch...upstream；其余是文件变更
        body = "\n".join(lines[:36])
        if len(lines) > 36:
            body += f"\n…(+{len(lines) - 36} more)"
        if not body:
            return f"branch: {name}\nclean working tree"
        return body
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _package_json_summary(cwd: Path) -> str:
    path = cwd / "package.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""

    parts: list[str] = []
    name = data.get("name")
    if name:
        parts.append(f"name: {name}")
    pkg_type = data.get("type")
    if pkg_type:
        parts.append(f"type: {pkg_type}")

    scripts = data.get("scripts")
    if isinstance(scripts, dict) and scripts:
        keys = list(scripts.keys())[:_MAX_SCRIPTS]
        listed = ", ".join(keys)
        extra = len(scripts) - len(keys)
        line = f"npm scripts: {listed}"
        if extra > 0:
            line += f" (+{extra})"
        parts.append(line)
        # 验收常用命令单独点出，方便模型直接 run
        for prefer in ("test", "lint", "build", "typecheck", "check"):
            if prefer in scripts:
                parts.append(f"建议验收: npm run {prefer}")
                break

    deps = data.get("dependencies")
    if isinstance(deps, dict) and deps:
        top = list(deps.keys())[:12]
        parts.append("dependencies: " + ", ".join(top))
    return "\n".join(parts)


def _pyproject_summary(cwd: Path) -> str:
    path = cwd / "pyproject.toml"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    parts: list[str] = []
    # 3.11+ 用 tomllib；更低版本用简单正则兜底（仍满足 3.10）
    try:
        import tomllib  # type: ignore[attr-defined]

        data = tomllib.loads(text)
        proj = data.get("project") or {}
        if isinstance(proj, dict):
            if proj.get("name"):
                parts.append(f"name: {proj['name']}")
            scripts = proj.get("scripts")
            if isinstance(scripts, dict) and scripts:
                parts.append("scripts: " + ", ".join(list(scripts.keys())[:_MAX_SCRIPTS]))
        tool = data.get("tool") or {}
        if isinstance(tool, dict):
            if "poetry" in tool:
                parts.append("build: poetry")
            if "ruff" in tool:
                parts.append("lint: ruff（见 pyproject）")
            if "pytest" in tool:
                parts.append("建议验收: pytest")
    except Exception:
        import re

        m = re.search(r'(?m)^\s*name\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            parts.append(f"name: {m.group(1)}")
        if "[tool.poetry]" in text:
            parts.append("build: poetry")
        if "[tool.ruff" in text or "[tool.ruff]" in text:
            parts.append("lint: ruff（见 pyproject）")
        if "[tool.pytest" in text or "pytest" in text.lower():
            parts.append("建议验收: pytest")
        if not parts:
            parts.append("存在 pyproject.toml")
    return "\n".join(parts)


def _detect_stack(cwd: Path) -> str:
    found = [name for name in _PROJECT_MARKERS if (cwd / name).is_file()]
    if (cwd / "src").is_dir():
        found.append("src/")
    if (cwd / "app").is_dir():
        found.append("app/")
    return ", ".join(found) if found else "(未识别到常见项目标记)"


def _rules_block(cwd: Path) -> str:
    chunks: list[str] = []
    used = 0
    for rel in _RULE_FILES:
        if used >= _MAX_RULES_CHARS:
            break
        path = cwd / rel
        if not path.is_file():
            continue
        budget = min(1_400, _MAX_RULES_CHARS - used)
        body = _read_text(path, budget)
        if not body:
            continue
        chunks.append(f"--- {rel} ---\n{body}")
        used += len(body)

    # .cursor/rules 下的 md/mdc（最多 3 个短文件）
    rules_dir = cwd / ".cursor" / "rules"
    if rules_dir.is_dir() and used < _MAX_RULES_CHARS:
        files = sorted(
            [p for p in rules_dir.iterdir() if p.suffix.lower() in {".md", ".mdc"} and p.is_file()]
        )[:3]
        for path in files:
            if used >= _MAX_RULES_CHARS:
                break
            budget = min(800, _MAX_RULES_CHARS - used)
            body = _read_text(path, budget)
            if not body:
                continue
            rel = path.relative_to(cwd).as_posix()
            chunks.append(f"--- {rel} ---\n{body}")
            used += len(body)

    return "\n\n".join(chunks)


def _readme_blurb(cwd: Path) -> str:
    for name in _README_CANDIDATES:
        path = cwd / name
        if path.is_file():
            body = _read_text(path, _MAX_README_CHARS)
            if body:
                return f"--- {name}（节选）---\n{body}"
    return ""


def gather_project_context(cwd: Path | None = None) -> str:
    """收集当前目录的项目上下文；无内容时返回空串。"""
    root = (cwd or Path.cwd()).resolve()
    sections: list[str] = []

    sections.append(f"标记文件: {_detect_stack(root)}")

    git = _git_snapshot(root)
    if git:
        sections.append("Git:\n" + git)

    pkg = _package_json_summary(root)
    if pkg:
        sections.append("Node/package.json:\n" + pkg)

    py = _pyproject_summary(root)
    if py:
        sections.append("Python/pyproject.toml:\n" + py)

    rules = _rules_block(root)
    if rules:
        sections.append("项目规则（必须遵守）:\n" + rules)

    readme = _readme_blurb(root)
    if readme:
        sections.append(readme)

    text = "\n\n".join(sections).strip()
    if len(text) > _MAX_CONTEXT_CHARS:
        text = text[:_MAX_CONTEXT_CHARS].rstrip() + "\n…[project context truncated]"
    return text
