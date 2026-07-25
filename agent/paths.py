"""路径辅助：Windows 路径解析、从用户输入抠绝对路径、切换工作目录。

模型有时会把 C:/Users/.../Desktop/foo 错误缩短成 foo。
resolve_path 尽量纠正；extract_abs_paths 从用户原话里抠出绝对路径提醒模型。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

def switch_cwd(new_dir: Path) -> None:
    """切换进程的当前工作目录。"""
    os.chdir(new_dir)

def _looks_like_path_input(path_candidate: str) -> bool:
    """粗判「用户是不是在输入一个路径」，避免把普通聊天内容误当路径。"""
    if not path_candidate:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", path_candidate):
        return True
    if path_candidate.startswith("\\\\") or path_candidate.startswith("//"):
        return True
    if path_candidate.startswith("~"):
        return True
    if (
        path_candidate.startswith("./")
        or path_candidate.startswith(".\\")
        or path_candidate.startswith("../")
        or path_candidate.startswith("..\\")
    ):
        return True
    return ("\\" in path_candidate or "/" in path_candidate) and not any(
        c in path_candidate for c in "，。！？：；、\n"
    )

def resolve_path(path: str) -> Path:
    """Resolve path; if model wrongly shortens Desktop projects, recover them."""
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        return Path.cwd()

    # Absolute Windows / UNC / POSIX
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("\\\\") or raw.startswith("/"):
        return Path(raw).expanduser().resolve(strict=False)

    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve(strict=False)

    cwd_cand = (Path.cwd() / p).resolve(strict=False)
    if cwd_cand.exists():
        return cwd_cand

    # Recover common mistake: C:/Users/.../Desktop/tttt -> tttt
    for alt in (
        Path.home() / "Desktop" / p,
        Path.home() / "桌面" / p,
        Path.home() / p,
    ):
        try:
            if alt.exists():
                return alt.resolve()
        except OSError:
            continue

    return cwd_cand

def extract_abs_paths(user_input: str) -> list[str]:
    """从用户输入里找出 Windows 绝对路径。"""
    # Stop before whitespace / Chinese punctuation so "C:\\a\\b，看下" works
    found = re.findall(
        r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n\s，。；、！？]+\\)*[^\\/:*?\"<>|\r\n\s，。；、！？]+",
        user_input,
    )
    # Deduplicate, keep order
    seen: set[str] = set()
    out: list[str] = []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

