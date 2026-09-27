"""工具实现与调度：tool_xxx 真正干活，execute_tool 按名字分发。

函数名约定：tool_<工具名>。返回值是字符串，会作为 role=tool 的内容发回模型。
成功一般以 OK: 开头，失败以 ERROR: / FAIL 开头。
工具声明见 agent.tools_schema；Agent 循环见 agent.loop。
"""

from __future__ import annotations

import atexit
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from agent.config import MAX_READ_CHARS, TAVILY_API_KEY
from agent.paths import resolve_path
from agent.render import show_note

# 本次会话里通过 run_command 启动的后台进程：pid -> {proc, cmd, cwd, log_path, started_at}
# 退出时会一并杀掉，不留开发服务器。
_BG_PROCESSES: dict[int, dict] = {}

# 一次 read_file 最多读几个文件；再多模型也消化不了，还会挤爆上下文
MAX_READ_BATCH = 10


def _read_one(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    budget: int = MAX_READ_CHARS,
) -> str:
    """读单个文件并排版成带行号的文本；budget 是本次允许返回的最大字符数。"""
    p = resolve_path(path)
    if not p.exists():
        return f"ERROR: file not found: {p}"
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = 1 if start_line is None else int(start_line)
    end = total if end_line is None else int(end_line)
    if start < 1:
        start = 1
    if end > total:
        end = total
    if total == 0:
        return f"{p} (empty file, 0 lines)"
    if start > total or start > end:
        return f"ERROR: invalid range {start}-{end} for file with {total} lines"

    # Numbered output for precise edits
    width = len(str(end))
    chunk = lines[start - 1 : end]
    body = "\n".join(f"{i:>{width}}|{line}" for i, line in enumerate(chunk, start))
    header = f"{p}  lines {start}-{end}/{total}\n"
    text = header + body
    if len(text) > budget:
        return text[:budget] + f"\n\n...[truncated, showing partial of {total} lines]"
    return text


def tool_read_file(
    path: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    paths: list[str] | None = None,
) -> str:
    """工具实现：读取文本文件，输出带行号，便于按行修改。
    传 paths=[…] 可一次读多个文件（各自整篇读，忽略 start_line/end_line）。"""
    if paths:
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            return "ERROR: paths must be an array of file paths"
        items = [str(x) for x in paths if str(x).strip()]
        if not items:
            return "ERROR: paths is empty"
        skipped = ""
        if len(items) > MAX_READ_BATCH:
            skipped = (
                f"\n\n...[skipped {len(items) - MAX_READ_BATCH} more file(s); "
                f"一次最多读 {MAX_READ_BATCH} 个]"
            )
            items = items[:MAX_READ_BATCH]
        # 总量仍受 MAX_READ_CHARS 约束，按文件数平分，避免一个大文件吃掉整个上下文
        budget = max(4_000, MAX_READ_CHARS // len(items))
        blocks = [f"===== [{i}/{len(items)}] {name} =====\n{_read_one(name, budget=budget)}"
                  for i, name in enumerate(items, 1)]
        return "\n\n".join(blocks) + skipped
    if not path:
        return "ERROR: path is required (or pass paths=[…])"
    return _read_one(path, start_line, end_line)

def _write_one(path: str, content: str) -> str:
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.is_file()
    old_len = p.stat().st_size if existed else 0
    p.write_text(content, encoding="utf-8", newline="\n")
    msg = f"OK: wrote {len(content)} chars to {p}"
    if existed and old_len > 200:
        msg += " | HINT: file already existed — for small fixes prefer edit_file next time"
    return msg

def tool_write_file(
    path: str | None = None,
    content: str | None = None,
    files: list[dict] | None = None,
) -> str:
    """工具实现：创建或整文件覆盖写入。传 files=[{path,content},…] 可批量写多个文件。"""
    if files:
        if not isinstance(files, list):
            return "ERROR: files must be an array of {path, content}"
        results: list[str] = []
        ok_count = 0
        for i, f in enumerate(files, 1):
            if not isinstance(f, dict) or not f.get("path"):
                results.append(f"[{i}] ERROR: item must be an object with path")
                continue
            r = _write_one(str(f["path"]), str(f.get("content") or ""))
            results.append(f"[{i}] {f['path']}: {r}")
            if r.startswith("OK"):
                ok_count += 1
        return f"{ok_count}/{len(files)} succeeded\n" + "\n".join(results)
    if not path:
        return "ERROR: path is required (or pass files=[…])"
    return _write_one(path, content if content is not None else "")

# ---------- 精确编辑：纯函数（供执行与 diff 预览共用） ----------

def apply_text_edit(text: str, old_text: str, new_text: str, replace_all: bool = False) -> tuple[str, str]:
    """对文本做一次 old→new 替换。返回 (new_text, "") 或 ("", error)。"""
    if old_text == new_text:
        return "", (
            "ERROR: no-op edit — old_text and new_text are identical. "
            "Change real code, or try a different fix."
        )
    if old_text == "":
        return "", "ERROR: old_text is empty"
    count = text.count(old_text)
    if count == 0:
        tip = ""
        key = old_text.strip().splitlines()[0][:40] if old_text.strip() else ""
        if key:
            for i, line in enumerate(text.splitlines(), 1):
                if key in line:
                    tip = f" | nearest line {i}: {line.strip()[:120]}"
                    break
        return "", f"ERROR: old_text not found in file{tip}"
    new = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
    if new == text:
        return "", "ERROR: no-op edit — file content unchanged after replace"
    return new, ""


def apply_line_edit(
    text: str, mode: str, start_line: int, end_line: int | None, content: str | None
) -> tuple[str, str, str]:
    """按行号编辑。返回 (new_text, summary, error)。

    mode=replace：替换 start..end（含）；insert：插到 start_line 之后（0=文件开头）；
    delete：删除 start..end。
    """
    lines = text.splitlines(keepends=True)
    n = len(lines)
    try:
        start = int(start_line)
        end = int(end_line) if end_line is not None else start
    except (TypeError, ValueError):
        return "", "", "ERROR: start_line/end_line must be integers"
    mode = (mode or "").strip().lower()

    if mode == "insert":
        if start < 0 or start > n:
            return "", "", f"ERROR: start_line {start} out of range (0..{n}) for insert"
        if not content:
            return "", "", "ERROR: content is empty"
        insert = content.splitlines(keepends=True)
        if insert and not insert[-1].endswith("\n"):
            insert[-1] += "\n"
        new_lines = lines[:start] + insert + lines[start:]
        return "".join(new_lines), f"inserted {len(insert)} line(s) after line {start}", ""

    if mode not in {"replace", "delete"}:
        return "", "", f"ERROR: mode must be replace | insert | delete, got {mode!r}"
    if start < 1 or end < start or start > n:
        return "", "", f"ERROR: invalid range {start}-{end} for file with {n} lines"
    end = min(end, n)
    if mode == "delete":
        new_lines = lines[: start - 1] + lines[end:]
        return "".join(new_lines), f"deleted lines {start}-{end} ({end - start + 1} lines)", ""
    insert = [] if not content else content.splitlines(keepends=True)
    if insert and not insert[-1].endswith("\n") and end < n:
        insert[-1] += "\n"
    new_lines = lines[: start - 1] + insert + lines[end:]
    return (
        "".join(new_lines),
        f"replaced lines {start}-{end} ({end - start + 1} lines) with {len(insert)} line(s)",
        "",
    )


def normalize_edits(args: dict) -> list[dict]:
    """把 edit_file 的参数统一成 [{path, old_text, new_text, replace_all}, …]。"""
    edits = args.get("edits")
    if isinstance(edits, list) and edits:
        out = []
        for e in edits:
            if isinstance(e, dict):
                out.append(
                    {
                        "path": e.get("path") or args.get("path"),
                        "old_text": e.get("old_text"),
                        "new_text": e.get("new_text"),
                        "replace_all": bool(e.get("replace_all", False)),
                    }
                )
        return out
    return [
        {
            "path": args.get("path"),
            "old_text": args.get("old_text"),
            "new_text": args.get("new_text"),
            "replace_all": bool(args.get("replace_all", False)),
        }
    ]


def preview_edit_file(args: dict) -> list[tuple[str, str, str, str]]:
    """不落盘地算出 edit_file 会把每个文件改成什么样。
    返回 [(path, old_text, new_text, error), …]，按文件聚合（同文件多处编辑顺序应用）。"""
    per_file: dict[str, dict] = {}
    for e in normalize_edits(args):
        path = e.get("path")
        if not path:
            continue
        p = resolve_path(str(path))
        key = str(p)
        slot = per_file.get(key)
        if slot is None:
            if not p.is_file():
                per_file[key] = {"old": "", "new": "", "err": f"ERROR: file not found: {p}"}
                continue
            original = p.read_text(encoding="utf-8", errors="replace")
            slot = per_file[key] = {"old": original, "new": original, "err": ""}
        if slot["err"]:
            continue
        new, err = apply_text_edit(
            slot["new"], str(e.get("old_text") or ""), str(e.get("new_text") or ""), e["replace_all"]
        )
        if err:
            slot["err"] = err
        else:
            slot["new"] = new
    return [(k, v["old"], v["new"], v["err"]) for k, v in per_file.items()]


def preview_edit_lines(args: dict) -> tuple[str, str, str, str]:
    """不落盘地算出 edit_lines 的结果。返回 (path, old_text, new_text, error)。"""
    p = resolve_path(str(args.get("path") or ""))
    if not p.is_file():
        return str(p), "", "", f"ERROR: file not found: {p}"
    original = p.read_text(encoding="utf-8", errors="replace")
    new, _summary, err = apply_line_edit(
        original,
        str(args.get("mode") or ""),
        args.get("start_line"),
        args.get("end_line"),
        args.get("content"),
    )
    return str(p), original, new, err


def tool_edit_file(
    path: str | None = None,
    old_text: str | None = None,
    new_text: str | None = None,
    replace_all: bool = False,
    edits: list[dict] | None = None,
) -> str:
    """工具实现：精确文本替换 old_text→new_text；传 edits=[…] 可一次改多处/多文件。"""
    args = {
        "path": path,
        "old_text": old_text,
        "new_text": new_text,
        "replace_all": replace_all,
        "edits": edits,
    }
    items = normalize_edits(args)
    if not items:
        return "ERROR: edits is empty"
    msgs: list[str] = []
    ok_count = 0
    for e in items:
        if not e.get("path") or e.get("old_text") is None or e.get("new_text") is None:
            msgs.append("ERROR: path / old_text / new_text are required")
            continue
        p = resolve_path(str(e["path"]))
        if not p.is_file():
            msgs.append(f"ERROR: file not found: {p}")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        new, err = apply_text_edit(text, str(e["old_text"]), str(e["new_text"]), e["replace_all"])
        if err:
            msgs.append(err)
            continue
        p.write_text(new, encoding="utf-8", newline="\n")
        n = text.count(str(e["old_text"])) if e["replace_all"] else 1
        msgs.append(f"OK: replaced {n} occurrence(s) in {p}")
        ok_count += 1
    if len(items) == 1:
        return msgs[0]
    lines = [f"[{i}] {e.get('path')}: {m}" for i, (e, m) in enumerate(zip(items, msgs), 1)]
    return f"{ok_count}/{len(items)} succeeded\n" + "\n".join(lines)


def tool_edit_lines(
    path: str,
    mode: str,
    start_line: int,
    end_line: int | None = None,
    content: str | None = None,
) -> str:
    """工具实现：按行号 replace / insert / delete。"""
    p = resolve_path(path)
    if not p.is_file():
        return f"ERROR: file not found: {p}"
    text = p.read_text(encoding="utf-8", errors="replace")
    new, summary, err = apply_line_edit(text, mode, start_line, end_line, content)
    if err:
        return err
    p.write_text(new, encoding="utf-8", newline="\n")
    return f"OK: {summary} in {p}"


def tool_delete_path(paths: list[str] | str) -> str:
    """工具实现：删除一个或多个文件（或空目录）。"""
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return "ERROR: paths is empty"
    results: list[str] = []
    ok_count = 0
    for i, path in enumerate(paths, 1):
        p = resolve_path(str(path))
        if not p.exists():
            results.append(f"[{i}] ERROR: path not found: {p}")
            continue
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                results.append(f"[{i}] ERROR: {p} is a non-empty directory — use run_command to remove it")
                continue
            results.append(f"[{i}] OK: removed empty directory {p}")
        else:
            p.unlink()
            results.append(f"[{i}] OK: deleted file {p}")
        ok_count += 1
    if len(paths) == 1:
        return results[0][4:]
    return f"{ok_count}/{len(paths)} succeeded\n" + "\n".join(results)

def tool_move_file(src: str, dest: str) -> str:
    """工具实现：移动或重命名文件/目录。"""
    s = resolve_path(src)
    d = resolve_path(dest)
    if not s.exists():
        return f"ERROR: source not found: {s}"
    d.parent.mkdir(parents=True, exist_ok=True)
    if d.exists():
        return f"ERROR: destination already exists: {d}"
    shutil.move(str(s), str(d))
    return f"OK: moved {s} -> {d}"

def tool_list_dir(path: str | None = None) -> str:
    """工具实现：列出目录下的文件和子目录，返回可直接复制的完整路径。"""
    p = resolve_path(path or ".")
    if not p.exists():
        return f"ERROR: path not found: {p}"
    if p.is_file():
        return f"[FILE] {p}"
    lines = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        kind = "[DIR]" if child.is_dir() else "[FILE]"
        lines.append(f"{kind} {child}")
    header = (
        f"[DIRECTORY] {p}\n"
        "Each entry below is: [TYPE] FULL_PATH. "
        "Use only FULL_PATH (after the marker) as a tool path."
    )
    return header + "\n" + ("\n".join(lines) if lines else "(empty)")

# ---------- 搜索文件内容 / 按名字找文件 ----------

def tool_glob_search(pattern: str, root: str | None = None) -> str:
    """工具实现：按 glob 模式找文件，例如 **/*.py。"""
    base = resolve_path(root or ".")
    matches = sorted(str(m) for m in base.glob(pattern))[:200]
    if not matches:
        return f"No matches for {pattern!r} under {base}"
    return "\n".join(matches)

def _grep_with_ripgrep(pattern: str, p: Path, glob: str | None) -> str | None:
    """尝试用 ripgrep 搜索；rg 不存在/不支持该正则时返回 None，让调用方退回 Python 实现。"""
    if not shutil.which("rg"):
        return None
    cmd = ["rg", "--line-number", "--no-heading", "--with-filename", "--color=never"]
    if glob:
        cmd += ["--glob", glob]
    cmd += ["-e", pattern, str(p)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
    except Exception:
        return None
    if proc.returncode == 2:
        # 正则语法 rg（Rust regex）不认，退回 Python re 实现
        return None
    if proc.returncode not in (0, 1):
        return f"ERROR: ripgrep: {(proc.stderr or proc.stdout).strip()[:300]}"
    out = proc.stdout.strip()
    if not out:
        return "No matches"
    lines = out.splitlines()
    if len(lines) > 80:
        lines = lines[:80] + ["...[truncated]"]
    return "\n".join(lines)

def tool_grep_search(pattern: str, path: str, glob: str | None = None) -> str:
    """工具实现：在文件/目录中搜索文本（正则）。优先用 ripgrep（更快、遵守 .gitignore），
    不可用或语法不兼容时退回内置的 Python 实现。"""
    p = resolve_path(path)
    if not p.exists():
        return f"ERROR: path not found: {p}"

    rg_result = _grep_with_ripgrep(pattern, p, glob)
    if rg_result is not None:
        return rg_result

    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"

    files: list[Path] = []
    if p.is_file():
        files = [p]
    elif p.is_dir():
        files = list(p.rglob(glob or "*"))
        files = [f for f in files if f.is_file()]

    hits: list[str] = []
    for f in files[:500]:
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{i}: {line[:200]}")
                    if len(hits) >= 80:
                        return "\n".join(hits) + "\n...[truncated]"
        except OSError:
            continue
    return "\n".join(hits) if hits else "No matches"

# ---------- 执行 Shell 命令 ----------

def prepare_command(command: str) -> str:
    """Make common interactive CLIs run non-interactively."""
    cmd = command.strip()
    # npx / npm create often wait for "Ok to proceed? (y)"
    if re.match(r"^npx(\s|$)", cmd, re.I) and "--yes" not in cmd and " -y " not in f" {cmd} ":
        cmd = re.sub(r"^npx\b", "npx --yes", cmd, count=1, flags=re.I)
    if re.match(r"^npm\s+create\b", cmd, re.I) and "--yes" not in cmd:
        cmd = re.sub(r"^npm\s+create\b", "npm create --yes", cmd, count=1, flags=re.I)
    return cmd


def _run_command_context(command: str, cwd: str | None = None) -> tuple[str, Path]:
    """规范命令与工作目录，并把开头的 cd path && 折叠进 cwd。"""
    work = resolve_path(cwd) if cwd else Path.cwd()
    cmd = prepare_command(command)
    cd_match = re.match(
        r"^cd\s+(?:/d\s+)?(?P<path>\"[^\"]+\"|'[^']+'|[^\s&]+)\s*&&\s*(?P<rest>.+)$",
        cmd,
        re.I,
    )
    if cd_match:
        work = resolve_path(cd_match.group("path").strip("\"'"))
        cmd = cd_match.group("rest").strip()
    return cmd, work


def preflight_run_command(command: str, cwd: str | None = None) -> str:
    """执行前拦截无效验收命令，避免弹确认后才发现项目/脚本不存在。"""
    cmd, work = _run_command_context(command, cwd)
    if not work.is_dir():
        return f"ERROR: command cwd does not exist or is not a directory: {work}"

    npm_run = re.match(r"^(npm|pnpm|yarn|bun)\s+run\s+([^\s;&|]+)", cmd, re.I)
    if npm_run:
        package_json = work / "package.json"
        if not package_json.is_file():
            return (
                f"SKIPPED: {work} 没有 package.json，不是可运行 npm scripts 的项目。"
                "不要猜测 lint/test/build 命令；使用已有文件级检查并如实总结验证范围。"
            )
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            return f"ERROR: cannot read valid package.json before running command: {e}"
        scripts = package_data.get("scripts") if isinstance(package_data, dict) else None
        script = npm_run.group(2)
        if not isinstance(scripts, dict) or script not in scripts:
            available = ", ".join(scripts.keys()) if isinstance(scripts, dict) and scripts else "(none)"
            return (
                f"SKIPPED: package.json 没有脚本 {script!r}；可用 scripts: {available}。"
                "只能运行项目实际声明的验收脚本。"
            )

    if re.search(r"\|\|\s*(?:true|exit\s+0)\b|;\s*exit\s+0\b", cmd, re.I):
        return (
            "ERROR: validation command masks failures with `|| true` / forced exit 0. "
            "Remove the failure-suppression suffix and run the real command."
        )
    return ""


if os.name == "nt":
    import ctypes

    def _windows_legacy_cp() -> str | None:
        """cmd.exe 内置命令（dir/tree/systeminfo 等）在输出被重定向（非真实控制台）时，
        是按系统 OEM 代码页（中文系统通常是 936=GBK）编码文本的，chcp 对重定向输出不起作用。
        查询真实的 OEM 代码页，供解码回退使用。"""
        try:
            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:
            return None
else:
    def _windows_legacy_cp() -> str | None:
        return None

def decode_subprocess_output(raw: bytes) -> str:
    """把子进程输出的原始字节解码成字符串。

    现代 CLI（git/node/npm/python…）大多直接写 UTF-8 字节，优先按 UTF-8 严格解码；
    只有解码失败时才回退到 Windows 的 OEM 代码页（修复 dir 等内置命令中文乱码导致模型
    看不懂输出、反复重跑同一条命令“确认”的问题），非 Windows 或探测失败则退回
    UTF-8（errors=replace）。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    legacy_cp = _windows_legacy_cp()
    if legacy_cp:
        try:
            return raw.decode(legacy_cp)
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode("utf-8", errors="replace")
LONG_RUNNING_PATTERNS = [
    r"npm\s+run\s+dev\b",
    r"npm\s+run\s+start\b",
    r"npm\s+start\b",
    r"pnpm\s+(run\s+)?dev\b",
    r"yarn\s+(run\s+)?dev\b",
    r"\bvite\b",
    r"\bnext\s+dev\b",
    r"\bnuxt\s+dev\b",
    r"python\s+-m\s+http\.server\b",
    r"npx\s+serve\b",
    r"\buvicorn\b",
    r"\bflask\s+run\b",
]

def is_long_running_command(command: str) -> bool:
    """判断是否是会一直运行的开发服务器命令（需后台启动）。"""
    return any(re.search(p, command, re.I) for p in LONG_RUNNING_PATTERNS)

def _run_command_background(cmd: str, work: Path, env: dict) -> str:
    """Start a long-running process, capture startup logs briefly, return."""
    show_note("后台启动中…")
    creationflags = 0
    if os.name == "nt":
        # 新进程组：不随 Ctrl+C 一起被误杀；不要用 DETACHED_PROCESS（会丢日志）
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    log_path = work / ".coder-dev-server.log"
    # 二进制模式：子进程直接写入这个文件描述符，不经过 Python 的文本编码层，
    # 用 "w"+encoding 会让人误以为写入内容是 UTF-8，实际取决于子进程自己的编码。
    log_f = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(work),
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )
    except Exception as e:
        log_f.close()
        return f"ERROR: failed to start background process: {e}"

    # Wait for server to print ready URL
    deadline = time.time() + 12
    out = ""
    while time.time() < deadline:
        time.sleep(0.4)
        try:
            out = decode_subprocess_output(log_path.read_bytes())
        except OSError:
            out = ""
        if re.search(r"Local:\s*https?://|localhost:\d+|ready in|Network:", out, re.I):
            break
        if proc.poll() is not None:
            break

    try:
        log_f.close()
    except Exception:
        pass

    out = out.strip() or "(no output yet)"
    if len(out) > 8_000:
        out = out[:8_000] + "\n...[truncated]"

    if proc.poll() is not None:
        return (
            f"exit={proc.returncode}\ncwd={work}\n"
            f"(command exited early, not kept in background)\n{out}"
        )

    _BG_PROCESSES[proc.pid] = {
        "proc": proc,
        "cmd": cmd,
        "cwd": str(work),
        "log_path": str(log_path),
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    urls = re.findall(r"https?://[\w\.-]+:\d+\S*", out)
    url_hint = f"\nurl={urls[0]}" if urls else "\nurl=(check log / default http://localhost:5173)"
    return (
        f"OK: started in background\n"
        f"pid={proc.pid}\ncwd={work}{url_hint}\n"
        f"log={log_path}\n"
        f"(dev server keeps running; do not wait for it to exit; "
        f"用 process(action=list|read|kill) 管理它)\n"
        f"--- startup log ---\n{out}"
    )

def tool_process(action: str, pid: int | None = None, tail_lines: int | None = None) -> str:
    """工具实现：后台进程管理。action=list 列出；read 读日志（需 pid）；kill 结束（需 pid）。"""
    act = (action or "").strip().lower()
    if act == "list":
        return _proc_list()
    if act in {"read", "log", "output"}:
        if pid is None:
            return "ERROR: pid is required for action=read"
        return _proc_read(pid, tail_lines)
    if act in {"kill", "stop"}:
        if pid is None:
            return "ERROR: pid is required for action=kill"
        return _proc_kill(pid)
    return f"ERROR: action must be list | read | kill, got {action!r}"

def _proc_list() -> str:
    """列出本次会话里通过 run_command 启动的后台进程。"""
    if not _BG_PROCESSES:
        return "(no background processes)"
    lines = []
    for pid, info in _BG_PROCESSES.items():
        proc = info["proc"]
        status = "running" if proc.poll() is None else f"exited(code={proc.returncode})"
        lines.append(
            f"pid={pid}  {status}  started={info['started_at']}\n"
            f"  cmd={info['cmd']}\n  cwd={info['cwd']}\n  log={info['log_path']}"
        )
    return "\n".join(lines)

def _proc_read(pid: int, tail_lines: int | None = None) -> str:
    """读取某个后台进程的日志；tail_lines 可只看最后 N 行。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "ERROR: pid must be an integer"
    info = _BG_PROCESSES.get(pid)
    if not info:
        return f"ERROR: no known background process with pid={pid}（先用 process(action=list) 查看）"
    log_path = Path(info["log_path"])
    try:
        text = decode_subprocess_output(log_path.read_bytes())
    except OSError as e:
        return f"ERROR: cannot read log: {e}"
    if tail_lines:
        lines = text.splitlines()
        text = "\n".join(lines[-int(tail_lines):])
    truncated_note = ""
    if len(text) > MAX_READ_CHARS:
        text = text[-MAX_READ_CHARS:]
        truncated_note = "...[truncated, showing tail]\n"
    proc = info["proc"]
    status = "running" if proc.poll() is None else f"exited(code={proc.returncode})"
    return f"pid={pid}  status={status}\nlog={log_path}\n\n{truncated_note}{text or '(empty log)'}"

def _proc_kill(pid: int) -> str:
    """结束某个后台进程（含子进程树）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "ERROR: pid must be an integer"
    info = _BG_PROCESSES.get(pid)
    if not info:
        return f"ERROR: no known background process with pid={pid}（先用 process(action=list) 查看）"
    proc = info["proc"]
    if proc.poll() is not None:
        _BG_PROCESSES.pop(pid, None)
        return f"OK: process {pid} had already exited (code={proc.returncode})"
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as e:
        return f"ERROR: failed to kill pid={pid}: {e}"
    _BG_PROCESSES.pop(pid, None)
    return f"OK: killed process {pid} and its child processes"


def kill_all_background() -> None:
    """结束本次会话启动的全部后台进程（退出时调用，不留开发服务器）。"""
    for pid in list(_BG_PROCESSES.keys()):
        try:
            _proc_kill(pid)
        except Exception:
            pass


atexit.register(kill_all_background)


def tool_run_command(command: str, cwd: str | None = None) -> str:
    """工具实现：在 shell 里执行命令；开发服务器会转后台。"""
    preflight = preflight_run_command(command, cwd)
    if preflight:
        return preflight
    cmd, work = _run_command_context(command, cwd)
    env = os.environ.copy()
    # Prevent hanging on interactive prompts (npx/npm/vite/git…)
    env.update(
        {
            "CI": "1",
            "npm_config_yes": "true",
            "NPM_CONFIG_YES": "true",
            "PIP_NO_INPUT": "1",
            "DEBIAN_FRONTEND": "noninteractive",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )

    if is_long_running_command(cmd):
        return _run_command_background(cmd, work, env)

    show_note("running…")
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(work),
            capture_output=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return (
            "ERROR: command timed out (180s). "
            "If this is a dev server, it should be detected as background; "
            "otherwise use non-interactive flags."
        )
    stdout = decode_subprocess_output(proc.stdout)
    stderr = decode_subprocess_output(proc.stderr)
    out = (stdout or "") + (("\n" + stderr) if stderr else "")
    out = out.strip() or "(no output)"
    if len(out) > 12_000:
        out = out[:12_000] + "\n...[truncated]"
    note = f"\n(executed: {cmd})" if cmd != command.strip() else ""
    return f"exit={proc.returncode}\ncwd={work}{note}\n{out}"

# ========================================================================
#  Todo / 计划清单（会话内内存，/clear_cache 时清空）
# ========================================================================

_TODO_STATUSES = ("pending", "in_progress", "completed", "cancelled")
# 每项: {"id": str, "content": str, "status": str}
_TODOS: list[dict[str, str]] = []


def clear_todos() -> None:
    """清空会话内 todo（main 在 /clear_cache 时调用）。"""
    _TODOS.clear()


def _format_todos() -> str:
    if not _TODOS:
        return "(empty todo list)"
    lines: list[str] = []
    for t in _TODOS:
        mark = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
            "cancelled": "[-]",
        }.get(t["status"], "[?]")
        lines.append(f"{mark} {t['id']}: {t['content']}  ({t['status']})")
    in_prog = sum(1 for t in _TODOS if t["status"] == "in_progress")
    done = sum(1 for t in _TODOS if t["status"] == "completed")
    header = f"todos={len(_TODOS)}  in_progress={in_prog}  completed={done}"
    return header + "\n" + "\n".join(lines)


def get_todos() -> list[dict[str, str]]:
    """当前会话 todo 的副本（会话保存用）。"""
    return [dict(t) for t in _TODOS]


def set_todos(items: list[dict]) -> None:
    """恢复会话时回填 todo。"""
    _TODOS[:] = [
        {"id": str(t.get("id", "")), "content": str(t.get("content", "")), "status": str(t.get("status", "pending"))}
        for t in items
        if isinstance(t, dict)
    ]


_TODO_CONTENT_KEYS = ("content", "task", "text", "title", "description", "name")
_TODO_EXAMPLE = (
    '{"todos":[{"id":"1","content":"创建目录","status":"in_progress"},'
    '{"id":"2","content":"写文件","status":"pending"}],"merge":false}'
)


def _todo_pick_content(raw: dict) -> str:
    """兼容模型常用的 content/task/title 等字段名。"""
    for key in _TODO_CONTENT_KEYS:
        val = raw.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _todo_coerce_item(raw: object, index: int) -> dict | str:
    """把一项 todo 收成 dict；失败时返回错误说明字符串。"""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return f"ERROR: todos[{index}] 是空字符串"
        return {
            "id": str(index + 1),
            "content": text,
            "status": "pending",
        }
    if not isinstance(raw, dict):
        return (
            f"ERROR: todos[{index}] 必须是对象（含 id/content/status），"
            f"不能是 {type(raw).__name__}。示例：{_TODO_EXAMPLE}"
        )

    # 允许 id 用数字
    tid = raw.get("id")
    if tid is None or str(tid).strip() == "":
        tid = str(index + 1)
    else:
        tid = str(tid).strip()

    content = _todo_pick_content(raw)
    status = str(raw.get("status") or "pending").strip().lower()
    # 常见同义词
    aliases = {
        "done": "completed",
        "complete": "completed",
        "finished": "completed",
        "doing": "in_progress",
        "progress": "in_progress",
        "working": "in_progress",
        "todo": "pending",
        "open": "pending",
        "cancel": "cancelled",
        "canceled": "cancelled",
    }
    status = aliases.get(status, status)
    return {"id": tid, "content": content, "status": status}


def tool_todo_write(todos: list, merge: bool = True) -> str:
    """创建或更新计划清单。

    merge=true（默认）：按 id 合并；已有项可只改 status/content；新 id 追加。
    merge=false：用本次列表整体替换。
    同一时刻最多 1 个 in_progress（多了会自动只保留第一个，其余改回 pending）。
    """
    # 模型有时把整个 todos 误传成 JSON 字符串
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            return (
                "ERROR: todos 必须是数组。示例："
                + _TODO_EXAMPLE
            )
    if not isinstance(todos, list) or not todos:
        return "ERROR: todos 必须是非空数组。示例：" + _TODO_EXAMPLE

    # 空清单时 merge 无意义，按整表替换处理，减少第一次调用踩坑
    if not _TODOS:
        merge = False

    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(todos):
        item = _todo_coerce_item(raw, i)
        if isinstance(item, str):
            return item
        tid = item["id"]
        content = item["content"]
        status = item["status"]
        if tid in seen_ids:
            return f"ERROR: 重复的 id: {tid}"
        seen_ids.add(tid)
        if status not in _TODO_STATUSES:
            return (
                f"ERROR: todos[{i}].status 无效: {status}；"
                f"允许: {', '.join(_TODO_STATUSES)}"
            )
        # 新建项缺 content 时，用 id 兜底，避免模型反复重试
        if not content and (not merge or tid not in {t["id"] for t in _TODOS}):
            content = tid
        normalized.append({"id": tid, "content": content, "status": status})

    if merge:
        by_id = {t["id"]: dict(t) for t in _TODOS}
        order = [t["id"] for t in _TODOS]
        for item in normalized:
            old = by_id.get(item["id"])
            if old:
                if not item["content"]:
                    item["content"] = old["content"]
                by_id[item["id"]] = item
            else:
                if not item["content"]:
                    item["content"] = item["id"]
                by_id[item["id"]] = item
                order.append(item["id"])
        _TODOS[:] = [by_id[i] for i in order if i in by_id]
    else:
        for item in normalized:
            if not item["content"]:
                item["content"] = item["id"]
        _TODOS[:] = normalized

    in_prog_ids = [t["id"] for t in _TODOS if t["status"] == "in_progress"]
    note = ""
    if len(in_prog_ids) > 1:
        keep = in_prog_ids[0]
        for t in _TODOS:
            if t["status"] == "in_progress" and t["id"] != keep:
                t["status"] = "pending"
        note = f"\nNOTE: 同时只能有 1 个 in_progress，已保留 {keep}，其余改回 pending。"
    elif not in_prog_ids and any(t["status"] == "pending" for t in _TODOS):
        # 首次建计划若全是 pending，自动把第一项标成进行中，减少多一轮调用
        for t in _TODOS:
            if t["status"] == "pending":
                t["status"] = "in_progress"
                note = f"\nNOTE: 已自动将 {t['id']} 标为 in_progress。"
                break

    return "OK:\n" + _format_todos() + note


class _HTMLStaticChecker(HTMLParser):
    """收集无需浏览器即可确认的 HTML/内联脚本问题。"""

    _LOCAL_REFS = {
        "script": {"src"},
        "link": {"href"},
        "img": {"src"},
        "source": {"src"},
        "audio": {"src"},
        "video": {"src", "poster"},
        "iframe": {"src"},
    }

    def __init__(self, path: Path) -> None:
        super().__init__(convert_charrefs=False)
        self.path = path
        self.issues: list[str] = []
        self.ids: dict[str, int] = {}
        self.scripts: list[tuple[int, bool, str]] = []
        self._script: tuple[int, bool, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        line, _ = self.getpos()
        names = [name.lower() for name, _ in attrs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            self.issues.append(f"line {line}: duplicate attribute(s): {', '.join(duplicates)}")
        values = {name.lower(): value or "" for name, value in attrs}
        element_id = values.get("id", "").strip()
        if element_id:
            if element_id in self.ids:
                self.issues.append(
                    f"line {line}: duplicate id {element_id!r} (first used at line {self.ids[element_id]})"
                )
            else:
                self.ids[element_id] = line
        for attr in self._LOCAL_REFS.get(tag, set()):
            self._check_local_ref(line, values.get(attr, ""))
        if tag == "script" and not values.get("src"):
            script_type = values.get("type", "").strip().lower()
            is_javascript = not script_type or script_type in {
                "text/javascript", "application/javascript", "module",
            }
            if is_javascript:
                self._script = (line, script_type == "module", [])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script is not None:
            line, is_module, chunks = self._script
            self.scripts.append((line, is_module, "".join(chunks)))
            self._script = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script[2].append(data)

    def close(self) -> None:
        super().close()
        if self._script is not None:
            line, is_module, chunks = self._script
            self.scripts.append((line, is_module, "".join(chunks)))
            self.issues.append(f"line {line}: unclosed <script> tag")
            self._script = None

    def _check_local_ref(self, line: int, value: str) -> None:
        ref = value.strip()
        if (
            not ref
            or ref.startswith(("#", "/", "//", "data:", "javascript:", "{{", "<%"))
            or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", ref)
        ):
            return
        clean = ref.split("#", 1)[0].split("?", 1)[0]
        if clean and not (self.path.parent / clean).exists():
            self.issues.append(f"line {line}: local resource not found: {ref}")


def _check_html(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    checker = _HTMLStaticChecker(path)
    try:
        checker.feed(text)
        checker.close()
    except Exception as e:
        return f"ERROR: invalid HTML near line {checker.getpos()[0]}: {type(e).__name__}: {e}"

    notes: list[str] = []
    if checker.scripts:
        node = shutil.which("node")
        if node:
            for line, is_module, script in checker.scripts:
                if not script.strip():
                    continue
                cmd = [node, "--check"]
                if is_module:
                    cmd.append("--input-type=module")
                cmd.append("-")
                try:
                    proc = subprocess.run(
                        cmd,
                        input=script,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except subprocess.TimeoutExpired:
                    checker.issues.append(f"line {line}: inline script syntax check timed out")
                    continue
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout).strip().splitlines()
                    summary = next(
                        (ln.strip() for ln in detail if "SyntaxError:" in ln),
                        next((ln.strip() for ln in detail if ln.strip()), "syntax error"),
                    )
                    stdin_pos = next(
                        (re.search(r"\[stdin\]:(\d+)", ln) for ln in detail if "[stdin]:" in ln),
                        None,
                    )
                    html_line = line + int(stdin_pos.group(1)) - 1 if stdin_pos else line
                    checker.issues.append(f"line {html_line}: inline JavaScript: {summary}")
        else:
            notes.append("node 不在 PATH，未检查内联 JavaScript")

    if checker.issues:
        shown = checker.issues[:20]
        extra = len(checker.issues) - len(shown)
        body = "\n".join(f"- {issue}" for issue in shown)
        if extra > 0:
            body += f"\n- ...还有 {extra} 个问题"
        return f"ERROR: HTML static check found {len(checker.issues)} issue(s) in {path}\n{body}"

    suffix = f"；{'；'.join(notes)}" if notes else ""
    return (
        f"OK: {path} — HTML static checks passed"
        f"（结构解析、重复 id/属性、本地资源、内联 JS）{suffix}。"
        "未执行浏览器运行时、交互或视觉验证；项目存在 build/lint/test 时继续运行对应命令。"
    )


def tool_check_syntax(path: str) -> str:
    """工具实现：对常见语言做一次快速语法自检（不代替真正的构建/测试/lint）。
    支持 .html / .py / .json / .js(x) / .mjs / .cjs；其它后缀提示改用 run_command。"""
    p = resolve_path(path)
    if not p.is_file():
        return f"ERROR: file not found: {p}"
    ext = p.suffix.lower()

    if ext == ".py":
        import py_compile

        try:
            py_compile.compile(str(p), doraise=True)
            return f"OK: {p} — no syntax errors"
        except py_compile.PyCompileError as e:
            return f"ERROR: {e}"

    if ext == ".json":
        try:
            json.loads(p.read_text(encoding="utf-8", errors="replace"))
            return f"OK: {p} — valid JSON"
        except json.JSONDecodeError as e:
            return f"ERROR: invalid JSON: {e}"

    if ext in {".html", ".htm"}:
        return _check_html(p)

    if ext in {".js", ".jsx", ".mjs", ".cjs"}:
        if not shutil.which("node"):
            return "ERROR: node not found on PATH, cannot check JS syntax"
        try:
            proc = subprocess.run(
                ["node", "--check", str(p)],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            return "ERROR: node --check timed out"
        if proc.returncode == 0:
            return f"OK: {p} — no syntax errors"
        return f"ERROR: {(proc.stderr or proc.stdout).strip()}"

    return (
        f"ERROR: unsupported extension {ext!r} for check_syntax "
        "(仅支持 .html/.htm/.py/.json/.js/.jsx/.mjs/.cjs)；"
        "其它语言请用 run_command 跑项目自带的 build/lint/typecheck 命令"
    )


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    """只服务本地页面，不把每个资源请求刷到终端。"""

    def log_message(self, format: str, *args) -> None:
        pass


def _launch_headless_browser(playwright):
    """优先复用系统 Chrome/Edge，再尝试 Playwright 自带 Chromium。"""
    failures: list[str] = []
    for label, kwargs in (
        ("Chrome", {"channel": "chrome"}),
        ("Edge", {"channel": "msedge"}),
        ("Chromium", {}),
    ):
        try:
            return playwright.chromium.launch(headless=True, **kwargs), label
        except Exception as e:
            failures.append(f"{label}: {str(e).splitlines()[0]}")
    raise RuntimeError("no usable browser; " + " | ".join(failures))


def _collect_browser_issues(target: str, delay: int, issues: list[str]) -> str:
    """运行页面并收集错误；确保先关闭浏览器，再停止 Playwright 驱动。"""
    from playwright.sync_api import sync_playwright

    browser = None
    browser_label = ""

    def add_issue(kind: str, detail: str) -> None:
        item = f"{kind}: {detail}".strip()
        if item not in issues:
            issues.append(item)

    try:
        with sync_playwright() as playwright:
            browser, browser_label = _launch_headless_browser(playwright)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 720})

                def on_console(message) -> None:
                    if message.type != "error":
                        return
                    location = message.location or {}
                    if str(location.get("url") or "").split("?", 1)[0].endswith("/favicon.ico"):
                        return
                    suffix = ""
                    if location.get("url"):
                        suffix = f" ({location['url']}:{location.get('lineNumber', 0)})"
                    add_issue("console.error", message.text + suffix)

                def on_request_failed(request) -> None:
                    if request.url.split("?", 1)[0].endswith("/favicon.ico"):
                        return
                    add_issue(
                        "request failed",
                        f"{request.method} {request.url} — {request.failure or 'unknown error'}",
                    )

                def on_response(response) -> None:
                    if response.status < 400 or response.url.split("?", 1)[0].endswith("/favicon.ico"):
                        return
                    add_issue("HTTP error", f"{response.status} {response.url}")

                def on_page_error(error) -> None:
                    detail = str(error)
                    stack = str(getattr(error, "stack", "") or "").strip()
                    if stack and stack != detail:
                        detail += "\n" + "\n".join(stack.splitlines()[:5])
                    add_issue("pageerror", detail)

                page.on("console", on_console)
                page.on("pageerror", on_page_error)
                page.on("requestfailed", on_request_failed)
                page.on("response", on_response)
                page.goto(target, wait_until="load", timeout=10_000)
                page.wait_for_timeout(delay)
            finally:
                browser.close()
                browser = None
    except Exception as e:
        add = f"{type(e).__name__}: {e}"
        if len(add) > 1200:
            add = add[:1200] + "…"
        add_issue("browser", add)
    return browser_label


def tool_check_webpage(
    path: str | None = None,
    url: str | None = None,
    wait_ms: int | None = None,
) -> str:
    """用无头浏览器运行本地 HTML 或 URL，收集控制台、页面与资源错误。"""
    if bool(path) == bool(url):
        return "ERROR: pass exactly one of path or url"

    server: ThreadingHTTPServer | None = None
    target = ""
    source = ""
    if path:
        page_path = resolve_path(path)
        if not page_path.is_file():
            return f"ERROR: HTML file not found: {page_path}"
        if page_path.suffix.lower() not in {".html", ".htm"}:
            return f"ERROR: check_webpage path must be .html/.htm, got {page_path.suffix!r}"
        handler = partial(_QuietStaticHandler, directory=str(page_path.parent))
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except OSError as e:
            return f"ERROR: cannot start temporary static server: {e}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        target = f"http://127.0.0.1:{port}/{urllib.parse.quote(page_path.name)}"
        source = str(page_path)
    else:
        target = str(url or "").strip()
        if not target.startswith(("http://", "https://")):
            return "ERROR: url must start with http:// or https://"
        source = target

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if server:
            server.shutdown()
            server.server_close()
        return (
            "ERROR: Playwright is not installed. Run `python -m pip install -r requirements.txt`, "
            "then ensure Chrome or Edge is installed."
        )

    delay = max(250, min(int(wait_ms or 2000), 10_000))
    issues: list[str] = []
    show_note("无头浏览器运行中…")
    try:
        browser_label = _collect_browser_issues(target, delay, issues)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

    if issues:
        # 本地临时端口每次随机且已在 finally 关闭；换回源文件路径，避免模型误判端口泄漏。
        display_issues = [item.replace(target, source) for item in issues]
        shown = display_issues[:20]
        body = "\n".join(f"- {item}" for item in shown)
        if len(display_issues) > len(shown):
            body += f"\n- ...还有 {len(display_issues) - len(shown)} 个问题"
        return (
            f"ERROR: webpage runtime check found {len(display_issues)} issue(s) in {source}"
            f" [{browser_label or 'browser'}]\n{body}\n"
            "NOTE: headless browser closed; temporary local server stopped. "
            "TIME_WAIT sockets after this check are normal and are not running servers."
        )
    return (
        f"OK: {source} — headless browser runtime check passed [{browser_label}, waited {delay}ms]. "
        "No console.error, uncaught page error, failed request, or HTTP 4xx/5xx was observed. "
        "Headless browser closed and temporary local server stopped. "
        "This does not verify visual appearance or every interaction."
    )


# ---------- 联网搜索与抓网页 ----------

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

def _decode_http_body(raw: bytes, content_type: str = "") -> str:
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    return raw.decode(charset, errors="replace")

def _http_get(
    url: str,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> str:
    """内部辅助：发 HTTP GET，返回解码后的文本。"""
    req_headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _decode_http_body(resp.read(), resp.headers.get("Content-Type", ""))

def _http_post_json(
    url: str,
    payload: dict,
    timeout: float = 25.0,
    headers: dict[str, str] | None = None,
) -> dict:
    """内部辅助：POST JSON，返回解析后的 dict。"""
    body = json.dumps(payload).encode("utf-8")
    req_headers = {
        "User-Agent": _DEFAULT_UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = _decode_http_body(resp.read(), resp.headers.get("Content-Type", ""))
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data

def _strip_html(html: str) -> str:
    """内部辅助：去掉 HTML 标签，留下大致正文。"""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def _clip_snippet(text: str, limit: int = 400) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"

def _search_tavily(query: str, max_results: int = 5) -> tuple[list[dict], str | None]:
    """调用 Tavily Search API。成功返回 (results, None)；失败返回 ([], error)。"""
    if not TAVILY_API_KEY:
        return [], "tavily: API key not configured"
    try:
        data = _http_post_json(
            "https://api.tavily.com/search",
            {
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": False,
            },
            timeout=25.0,
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
        )
    except Exception as e:
        return [], f"tavily: {type(e).__name__}: {e}"

    out: list[dict] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip() or url
        snippet = _clip_snippet(str(item.get("content") or ""))
        if url.startswith("http"):
            out.append({"title": title, "url": url, "snippet": snippet, "source": "tavily"})
        if len(out) >= max_results:
            break
    if not out:
        return [], "tavily: no hits"
    return out, None

def tool_web_search(query: str) -> str:
    """工具实现：联网搜索（Tavily）。"""
    q = (query or "").strip()
    if not q:
        return "ERROR: empty query"
    if not TAVILY_API_KEY:
        return (
            "ERROR: no search API key configured\n"
            "Set TAVILY_API_KEY env var, or add tavily_api_key to config.json.\n"
            "If you know a docs URL, try fetch_url directly."
        )

    show_note("searching…")

    results, err = _search_tavily(q)

    lines = [f"query={q}", "backend=tavily", ""]
    if results:
        lines.append("== web results ==")
        for i, item in enumerate(results, 1):
            lines.append(f"{i}. [{item['source']}] {item['title']}")
            lines.append(f"   {item['url']}")
            if item.get("snippet"):
                lines.append(f"   {item['snippet']}")
        lines.append("")
        lines.append("Next: fetch_url a relevant link, then apply a DIFFERENT fix.")
    else:
        lines.append("== web results ==")
        lines.append("ERROR: tavily search failed or returned no hits")
        if err:
            lines.append(f"detail: {err}")
        lines.append("If you know a docs URL, try fetch_url directly.")
    return "\n".join(lines)

def tool_fetch_url(url: str) -> str:
    """工具实现：抓取指定网页正文。"""
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return "ERROR: url must start with http:// or https://"
    show_note("fetching…")
    try:
        html = _http_get(u, timeout=25)
    except Exception as e:
        return f"ERROR: fetch_url failed: {type(e).__name__}: {e}"
    text = _strip_html(html)
    if len(text) > 12_000:
        text = text[:12_000] + "\n...[truncated]"
    return f"url={u}\n\n{text or '(empty page)'}"

# ========================================================================
#  工具调度：把模型返回的 name + arguments 映射到上面的 tool_xxx
# ========================================================================

# 自动收集本模块所有 tool_xxx 函数：注册名 = 去掉 tool_ 前缀后的函数名，
# 与 tools_schema.TOOLS 里的工具名一一对应。新增工具只需写 tool_<name> + 补 schema。
_TOOL_FUNCS: dict[str, Callable[..., str]] = {
    fn_name[len("tool_"):]: fn
    for fn_name, fn in sorted(globals().items())
    if fn_name.startswith("tool_") and callable(fn)
}

def execute_tool(name: str, args: dict) -> str:
    """
    工具总调度：根据模型给出的工具名 name，把参数 args 交给对应的 tool_xxx 函数。

    模型不会直接跑 Python；它只返回「想调用哪个工具、参数是什么」。
    真正执行发生在这里。参数按函数签名过滤：模型多给的字段忽略，
    少给必填字段则返回可读的错误让模型自行纠正。
    """
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return f"ERROR: unknown tool {name}"
    params = inspect.signature(fn).parameters
    kwargs = {k: v for k, v in args.items() if k in params}
    missing = [
        p.name
        for p in params.values()
        if p.default is inspect.Parameter.empty and p.name not in kwargs
    ]
    if missing:
        return f"ERROR: missing argument(s): {', '.join(missing)}"
    try:
        return fn(**kwargs)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

