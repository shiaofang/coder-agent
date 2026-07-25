"""工具实现与调度：tool_xxx 真正干活，execute_tool 按名字分发。

函数名约定：tool_<工具名>。返回值是字符串，会作为 role=tool 的内容发回模型。
成功一般以 OK: 开头，失败以 ERROR: / FAIL 开头。
工具声明见 agent.tools_schema；Agent 循环见 agent.loop。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from agent.config import MAX_READ_CHARS
from agent.paths import resolve_path
from agent.terminal import C, paint

# 本次会话里通过 run_command 启动的后台进程：pid -> {proc, cmd, cwd, log_path, started_at}
# 只在内存里维护，agent 进程退出后自然清空。
_BG_PROCESSES: dict[int, dict] = {}

def tool_read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """工具实现：读取文本文件，输出带行号，便于按行修改。"""
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
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n\n...[truncated, showing partial of {total} lines]"
    return text

def tool_write_file(path: str, content: str) -> str:
    """工具实现：创建或整文件覆盖写入。小改动应优先用 edit_file。"""
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.is_file()
    old_len = p.stat().st_size if existed else 0
    p.write_text(content, encoding="utf-8", newline="\n")
    msg = f"OK: wrote {len(content)} chars to {p}"
    if existed and old_len > 200:
        msg += (
            " | HINT: file already existed — for small fixes prefer edit_file/"
            "replace_lines next time instead of full rewrite"
        )
    return msg

def tool_write_files(files: list[dict]) -> str:
    """工具实现：一次性创建/覆盖写入多个文件，适合"批量建 N 个文件"的场景，
    避免逐个 write_file 来回确认。每条同 write_file 语义，单条失败不影响其它条。"""
    if not files:
        return "ERROR: files is empty"
    results: list[str] = []
    ok_count = 0
    for i, f in enumerate(files, 1):
        if not isinstance(f, dict):
            results.append(f"[{i}] ERROR: file item must be an object")
            continue
        try:
            path = f["path"]
        except KeyError as exc:
            results.append(f"[{i}] ERROR: missing field {exc}")
            continue
        r = tool_write_file(path, f.get("content", ""))
        results.append(f"[{i}] {path}: {r}")
        if r.startswith("OK"):
            ok_count += 1
    return f"{ok_count}/{len(files)} succeeded\n" + "\n".join(results)

def tool_edit_file(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> str:
    """工具实现：把文件中的 old_text 精确替换成 new_text。"""
    p = resolve_path(path)
    if not p.is_file():
        return f"ERROR: file not found: {p}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if old_text == new_text:
        return (
            "ERROR: no-op edit — old_text and new_text are identical. "
            "Change real code, or web_search the error and try a different fix."
        )
    count = text.count(old_text)
    if count == 0:
        # Help model with nearby lines containing a distinctive snippet
        tip = ""
        key = old_text.strip().splitlines()[0][:40] if old_text.strip() else ""
        if key:
            for i, line in enumerate(text.splitlines(), 1):
                if key in line:
                    tip = f" | nearest line {i}: {line.strip()[:120]}"
                    break
        return f"ERROR: old_text not found in file{tip}"
    if replace_all:
        new = text.replace(old_text, new_text)
        n = count
    else:
        new = text.replace(old_text, new_text, 1)
        n = 1
    if new == text:
        return "ERROR: no-op edit — file content unchanged after replace"
    p.write_text(new, encoding="utf-8", newline="\n")
    return f"OK: replaced {n} occurrence(s) in {p}"

def tool_multi_edit(edits: list[dict]) -> str:
    """工具实现：一次性对多个文件/多处内容做精确替换（每条同 edit_file 语义），
    减少跨文件重构时来回调用的次数。逐条执行，某条失败不影响其它条。"""
    if not edits:
        return "ERROR: edits is empty"
    results: list[str] = []
    ok_count = 0
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            results.append(f"[{i}] ERROR: edit item must be an object")
            continue
        try:
            path = e["path"]
            old_text = e["old_text"]
            new_text = e["new_text"]
        except KeyError as exc:
            results.append(f"[{i}] ERROR: missing field {exc}")
            continue
        r = tool_edit_file(path, old_text, new_text, bool(e.get("replace_all", False)))
        results.append(f"[{i}] {path}: {r}")
        if r.startswith("OK"):
            ok_count += 1
    summary = f"{ok_count}/{len(edits)} succeeded"
    return summary + "\n" + "\n".join(results)

def tool_replace_lines(path: str, start_line: int, end_line: int, new_content: str) -> str:
    """工具实现：按行号区间替换一段内容。"""
    p = resolve_path(path)
    if not p.is_file():
        return f"ERROR: file not found: {p}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    n = len(lines)
    try:
        start = int(start_line)
        end = int(end_line)
    except (TypeError, ValueError):
        return "ERROR: start_line/end_line must be integers"
    if start < 1 or end < start or start > n:
        return f"ERROR: invalid range {start}-{end} for file with {n} lines"
    end = min(end, n)
    insert = [] if new_content == "" else new_content.splitlines(keepends=True)
    if insert and not insert[-1].endswith("\n") and end < n:
        insert[-1] += "\n"
    new_lines = lines[: start - 1] + insert + lines[end:]
    p.write_text("".join(new_lines), encoding="utf-8", newline="\n")
    return f"OK: replaced lines {start}-{end} ({end - start + 1} lines) with {len(insert)} line(s) in {p}"

def tool_insert_lines(path: str, after_line: int, content: str) -> str:
    """工具实现：在指定行之后插入文本。"""
    p = resolve_path(path)
    if not p.is_file():
        return f"ERROR: file not found: {p}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    try:
        after = int(after_line)
    except (TypeError, ValueError):
        return "ERROR: after_line must be an integer"
    if after < 0 or after > len(lines):
        return f"ERROR: after_line {after} out of range (0..{len(lines)})"
    if content == "":
        return "ERROR: content is empty"
    insert = content.splitlines(keepends=True)
    if insert and not insert[-1].endswith("\n"):
        insert[-1] += "\n"
    new_lines = lines[:after] + insert + lines[after:]
    p.write_text("".join(new_lines), encoding="utf-8", newline="\n")
    return f"OK: inserted {len(insert)} line(s) after line {after} in {p}"

def tool_delete_lines(path: str, start_line: int, end_line: int) -> str:
    """工具实现：按行号删除一段内容。"""
    p = resolve_path(path)
    if not p.is_file():
        return f"ERROR: file not found: {p}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    n = len(lines)
    try:
        start = int(start_line)
        end = int(end_line)
    except (TypeError, ValueError):
        return "ERROR: start_line/end_line must be integers"
    if start < 1 or end < start or start > n:
        return f"ERROR: invalid range {start}-{end} for file with {n} lines"
    end = min(end, n)
    new_lines = lines[: start - 1] + lines[end:]
    p.write_text("".join(new_lines), encoding="utf-8", newline="\n")
    return f"OK: deleted lines {start}-{end} ({end - start + 1} lines) in {p}"

def tool_delete_file(path: str) -> str:
    """工具实现：删除文件（不能删目录）。"""
    p = resolve_path(path)
    if not p.exists():
        return f"ERROR: path not found: {p}"
    if p.is_dir():
        return f"ERROR: {p} is a directory — use run_command to remove dirs if needed"
    p.unlink()
    return f"OK: deleted file {p}"

def tool_delete_files(paths: list[str]) -> str:
    """工具实现：一次性删除多个文件，适合"批量删文件"的场景。
    单条失败不影响其它条（比如某个文件已经不存在）。"""
    if not paths:
        return "ERROR: paths is empty"
    results: list[str] = []
    ok_count = 0
    for i, path in enumerate(paths, 1):
        r = tool_delete_file(path)
        results.append(f"[{i}] {path}: {r}")
        if r.startswith("OK"):
            ok_count += 1
    return f"{ok_count}/{len(paths)} succeeded\n" + "\n".join(results)

def tool_move_file(src: str, dest: str) -> str:
    """工具实现：移动或重命名文件/目录。"""
    import shutil

    s = resolve_path(src)
    d = resolve_path(dest)
    if not s.exists():
        return f"ERROR: source not found: {s}"
    d.parent.mkdir(parents=True, exist_ok=True)
    if d.exists():
        return f"ERROR: destination already exists: {d}"
    shutil.move(str(s), str(d))
    return f"OK: moved {s} -> {d}"

def tool_mkdir(path: str) -> str:
    """工具实现：创建目录（含中间目录）。"""
    p = resolve_path(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"OK: directory ready {p}"

def tool_list_dir(path: str | None = None) -> str:
    """工具实现：列出目录下的文件和子目录。"""
    p = resolve_path(path or ".")
    if not p.exists():
        return f"ERROR: path not found: {p}"
    if p.is_file():
        return f"FILE: {p}"
    lines = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        kind = "dir " if child.is_dir() else "file"
        lines.append(f"{kind}  {child.name}")
    return f"{p}\n" + ("\n".join(lines) if lines else "(empty)")
# ---------- 6.2 搜索文件内容 / 按名字找文件 ----------

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

# ---------- 6.3 执行 Shell 命令 ----------
def prepare_command(command: str) -> str:
    """Make common interactive CLIs run non-interactively."""
    cmd = command.strip()
    # npx / npm create often wait for "Ok to proceed? (y)"
    if re.match(r"^npx(\s|$)", cmd, re.I) and "--yes" not in cmd and " -y " not in f" {cmd} ":
        cmd = re.sub(r"^npx\b", "npx --yes", cmd, count=1, flags=re.I)
    if re.match(r"^npm\s+create\b", cmd, re.I) and "--yes" not in cmd:
        cmd = re.sub(r"^npm\s+create\b", "npm create --yes", cmd, count=1, flags=re.I)
    return cmd
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

def tool_run_command_background(cmd: str, work: Path, env: dict) -> str:
    """Start a long-running process, capture startup logs briefly, return."""
    print(paint("  ⎿  ", C.DIM) + paint("后台启动中…", C.DIM, C.SPINNER_LABEL), flush=True)
    creationflags = 0
    if os.name == "nt":
        # 新进程组：不随 Ctrl+C 一起被误杀；不要用 DETACHED_PROCESS（会丢日志）
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    log_path = work / ".coder-dev-server.log"
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
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
            out = log_path.read_text(encoding="utf-8", errors="replace")
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
        f"用 list_processes/read_process_output/kill_process 管理它)\n"
        f"--- startup log ---\n{out}"
    )

def tool_list_processes() -> str:
    """工具实现：列出本次会话里通过 run_command 启动的后台进程。"""
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

def tool_read_process_output(pid: int, tail_lines: int | None = None) -> str:
    """工具实现：读取某个后台进程的日志；tail_lines 可只看最后 N 行。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "ERROR: pid must be an integer"
    info = _BG_PROCESSES.get(pid)
    if not info:
        return f"ERROR: no known background process with pid={pid}（先用 list_processes 查看）"
    log_path = Path(info["log_path"])
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
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

def tool_kill_process(pid: int) -> str:
    """工具实现：结束某个后台进程（含子进程树）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "ERROR: pid must be an integer"
    info = _BG_PROCESSES.get(pid)
    if not info:
        return f"ERROR: no known background process with pid={pid}（先用 list_processes 查看）"
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

def tool_run_command(command: str, cwd: str | None = None) -> str:
    """工具实现：在 shell 里执行命令；开发服务器会转后台。"""
    work = resolve_path(cwd) if cwd else Path.cwd()
    if not work.exists():
        work.mkdir(parents=True, exist_ok=True)
    cmd = prepare_command(command)
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

    # Strip leading "cd path &&" into cwd when possible
    cd_match = re.match(r"^cd\s+(?:/d\s+)?(?P<path>\"[^\"]+\"|'[^']+'|[^\s&]+)\s*&&\s*(?P<rest>.+)$", cmd, re.I)
    if cd_match:
        work = resolve_path(cd_match.group("path").strip("\"'"))
        if not work.exists():
            work.mkdir(parents=True, exist_ok=True)
        cmd = cd_match.group("rest").strip()

    if is_long_running_command(cmd):
        return tool_run_command_background(cmd, work, env)

    print(paint("  ⎿  ", C.DIM) + paint("running…", C.DIM, C.SPINNER_LABEL), flush=True)
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(work),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    out = out.strip() or "(no output)"
    if len(out) > 12_000:
        out = out[:12_000] + "\n...[truncated]"
    note = f"\n(executed: {cmd})" if cmd != command.strip() else ""
    return f"exit={proc.returncode}\ncwd={work}{note}\n{out}"

def tool_get_datetime() -> str:
    """工具实现：返回当前本地日期时间。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")

def tool_check_syntax(path: str) -> str:
    """工具实现：对常见语言做一次快速语法自检（不代替真正的构建/测试/lint）。
    支持 .py / .json / .js(x) / .mjs / .cjs；其它后缀提示改用 run_command。"""
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
        "(仅支持 .py/.json/.js/.jsx/.mjs/.cjs)；"
        "其它语言请用 run_command 跑项目自带的 build/lint/typecheck 命令"
    )

# ---------- 6.4 联网搜索与抓网页 ----------

def _http_get(url: str, timeout: float = 20.0) -> str:
    """内部辅助：发 HTTP GET，返回解码后的文本。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = "utf-8"
        ctype = resp.headers.get("Content-Type", "")
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            charset = m.group(1)
        return raw.decode(charset, errors="replace")

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

def tool_web_search(query: str) -> str:
    """工具实现：联网搜索（DuckDuckGo / Bing / 百度兜底）。"""
    q = (query or "").strip()
    if not q:
        return "ERROR: empty query"
    print(paint("  ⎿  ", C.DIM) + paint("searching…", C.DIM, C.SPINNER_LABEL), flush=True)

    results: list[tuple[str, str, str]] = []  # title, url, source
    errors: list[str] = []

    # 实测本机网络下各后端延迟差异很大：ddg ~10s 且结果可用；bing ~30s；
    # baidu 常返回反爬验证页（几百字节，提不出结果）。因此把 ddg 放第一位，
    # 并给每个后端按实际延迟设置足够的超时，而不是统一 12s（太短，几乎必超时）。
    backends = [
        (
            "ddg",
            "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q),
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            18,
        ),
        (
            "bing",
            "https://cn.bing.com/search?q=" + urllib.parse.quote(q),
            r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            35,
        ),
        (
            "baidu",
            "https://www.baidu.com/s?wd=" + urllib.parse.quote(q),
            r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            18,
        ),
    ]

    for source, url, pattern, backend_timeout in backends:
        try:
            html = _http_get(url, timeout=backend_timeout)
        except Exception as e:
            errors.append(f"{source}: {type(e).__name__}")
            continue
        for m in re.finditer(pattern, html, re.I | re.S):
            href = m.group(1)
            title = _strip_html(m.group(2))
            if source == "ddg":
                um = re.search(r"uddg=([^&]+)", href)
                if um:
                    href = urllib.parse.unquote(um.group(1))
            if not title:
                continue
            if href.startswith("//"):
                href = "https:" + href
            if href.startswith("http"):
                results.append((title, href, source))
            if len(results) >= 5:
                break
        if results:
            break

    lines = [f"query={q}", ""]
    if results:
        lines.append("== web results ==")
        for i, (title, href, source) in enumerate(results, 1):
            lines.append(f"{i}. [{source}] {title}")
            lines.append(f"   {href}")
        lines.append("")
        lines.append("Next: fetch_url a relevant link, then apply a DIFFERENT fix.")
    else:
        lines.append("== web results ==")
        lines.append("ERROR: all search backends failed or returned no hits")
        if errors:
            lines.append("backends: " + "; ".join(errors))
        lines.append("If you know a docs URL, try fetch_url directly.")
    return "\n".join(lines)

def tool_fetch_url(url: str) -> str:
    """工具实现：抓取指定网页正文。"""
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return "ERROR: url must start with http:// or https://"
    print(paint("  ⎿  ", C.DIM) + paint("fetching…", C.DIM, C.SPINNER_LABEL), flush=True)
    try:
        html = _http_get(u, timeout=25)
    except Exception as e:
        return f"ERROR: fetch_url failed: {type(e).__name__}: {e}"
    text = _strip_html(html)
    if len(text) > 12_000:
        text = text[:12_000] + "\n...[truncated]"
    return f"url={u}\n\n{text or '(empty page)'}"

# ========================================================================
#  第 7 区：工具调度

# ========================================================================
# 把模型返回的 name + arguments 映射到上面的 tool_xxx。
#

def execute_tool(name: str, args: dict) -> str:
    """
    工具总调度：根据模型给出的工具名 name，把参数 args 交给对应的 tool_xxx 函数。

    模型不会直接跑 Python；它只返回「想调用哪个工具、参数是什么」。
    真正执行发生在这里。
    """
    try:
        if name == "read_file":
            return tool_read_file(
                args["path"],
                args.get("start_line"),
                args.get("end_line"),
            )
        if name == "write_file":
            return tool_write_file(args["path"], args.get("content", ""))
        if name == "write_files":
            return tool_write_files(args["files"])
        if name == "edit_file":
            return tool_edit_file(
                args["path"],
                args["old_text"],
                args["new_text"],
                bool(args.get("replace_all", False)),
            )
        if name == "multi_edit":
            return tool_multi_edit(args["edits"])
        if name == "replace_lines":
            return tool_replace_lines(
                args["path"],
                args["start_line"],
                args["end_line"],
                args.get("new_content", ""),
            )
        if name == "insert_lines":
            return tool_insert_lines(args["path"], args["after_line"], args.get("content", ""))
        if name == "delete_lines":
            return tool_delete_lines(args["path"], args["start_line"], args["end_line"])
        if name == "delete_file":
            return tool_delete_file(args["path"])
        if name == "delete_files":
            return tool_delete_files(args["paths"])
        if name == "move_file":
            return tool_move_file(args["src"], args["dest"])
        if name == "mkdir":
            return tool_mkdir(args["path"])
        if name == "list_dir":
            return tool_list_dir(args.get("path"))
        if name == "glob_search":
            return tool_glob_search(args["pattern"], args.get("root"))
        if name == "grep_search":
            return tool_grep_search(args["pattern"], args["path"], args.get("glob"))
        if name == "run_command":
            return tool_run_command(args["command"], args.get("cwd"))
        if name == "list_processes":
            return tool_list_processes()
        if name == "read_process_output":
            return tool_read_process_output(args["pid"], args.get("tail_lines"))
        if name == "kill_process":
            return tool_kill_process(args["pid"])
        if name == "check_syntax":
            return tool_check_syntax(args["path"])
        if name == "web_search":
            return tool_web_search(args["query"])
        if name == "fetch_url":
            return tool_fetch_url(args["url"])
        if name == "get_datetime":
            return tool_get_datetime()
        return f"ERROR: unknown tool {name}"
    except KeyError as e:
        return f"ERROR: missing argument {e}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

