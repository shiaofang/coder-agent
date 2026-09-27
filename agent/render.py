"""终端渲染（rich）：流式 markdown 回复、思考折叠、工具调用/结果、diff 预览、统计行。

所有「往终端画东西」的逻辑集中在这里；按键读取与确认菜单仍在 agent.terminal。
"""

from __future__ import annotations

import difflib
import time
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from agent import config
from agent.paths import resolve_path


def _prepare_windows_console() -> None:
    """必须在创建 rich Console 之前：开启 VT 转义、stdout 切 UTF-8，
    否则 rich 会走 legacy Windows 渲染路径（GBK 编码下画不出 ⎿ █ 等字符）。"""
    import sys

    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle_id in (-11, -12):
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


_prepare_windows_console()
console = Console(highlight=False, soft_wrap=True)

# 工具结果默认显示的行数（/verbose 后全量）
_RESULT_LINES_DEFAULT = 5
_RESULT_LINES_COMMAND = 12
_DIFF_MAX_LINES = 60
_NEW_FILE_PREVIEW_LINES = 20
_THINK_TAIL_LINES = 4

# 段落边界之后若紧跟这些开头，视为同一块（避免列表被拆成两段导致编号重排）
_CONTINUATION_PREFIXES = ("- ", "* ", "+ ", " ", "\t", "|", ">")


def info(msg: str) -> None:
    console.print(f"  [cyan]✓[/] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [yellow]⚠[/] {msg}")


def error(msg: str) -> None:
    console.print(f"[bold red]✗[/] {msg}")


def dim(msg: str) -> None:
    console.print(f"[dim]{msg}[/]")


def _fmt_k(n: int | float) -> str:
    n = int(n)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# ========================================================================
#  流式回复渲染
# ========================================================================

class StreamRenderer:
    """一次 chat_once 的终端渲染：spinner → 思考（折叠）→ markdown 回复。"""

    def __init__(self, status: str = "Working…") -> None:
        self._status = console.status(f"[dim]{status}[/]", spinner="dots")
        self._status_on = False
        self._live: Live | None = None
        self._mode = ""  # "" | "think" | "reply"
        self._reasoning: list[str] = []
        self._think_started = 0.0
        self._think_spinner = Spinner("dots", text=" 思考中…", style="magenta")
        self._pending = ""  # 尚未 flush 的 markdown 段落
        self._replied = False

    # ---- 生命周期 ----
    def start(self) -> None:
        self._status.start()
        self._status_on = True

    def _stop_status(self) -> None:
        if self._status_on:
            self._status.stop()
            self._status_on = False

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    # ---- 思考 ----
    def on_reasoning(self, text: str) -> None:
        self._stop_status()
        if self._mode != "think":
            self._end_reply()
            self._mode = "think"
            self._think_started = time.time()
            self._live = Live(console=console, transient=True, refresh_per_second=8)
            self._live.start()
        self._reasoning.append(text)
        if self._live is not None:
            full = "".join(self._reasoning)
            tail = full.strip().splitlines()[-_THINK_TAIL_LINES:]
            body = Text("\n".join("  " + ln for ln in tail), style="dim italic")
            self._live.update(Group(self._think_spinner, body))

    def _end_thinking(self) -> None:
        if self._mode != "think":
            return
        self._stop_live()
        full = "".join(self._reasoning)
        secs = time.time() - self._think_started
        show_body = config.VERBOSE
        if show_body and full.strip():
            console.print(Text("∴ 深度思考", style="magenta"))
            console.print(Text("\n".join("  " + ln for ln in full.strip().splitlines()), style="dim italic"))
        console.print(f"[magenta]∴[/] [dim]思考 {_fmt_k(len(full))} 字 · {secs:.1f}s[/]")
        self._mode = ""

    # ---- 回复 ----
    def on_content(self, text: str) -> None:
        self._stop_status()
        self._end_thinking()
        if self._mode != "reply":
            self._mode = "reply"
            if not self._replied:
                console.print("[green]⏺[/]")
                self._replied = True
            self._live = Live(console=console, transient=True, refresh_per_second=10)
            self._live.start()
        self._pending += text
        self._flush_paragraphs()
        if self._live is not None:
            self._live.update(Markdown(self._pending))

    def _flush_paragraphs(self) -> None:
        """把已完成的段落（空行分隔、不在代码块内）打印成 markdown，只保留末尾未完段落。"""
        boundary = -1
        in_fence = False
        pos = 0
        lines = self._pending.splitlines(keepends=True)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
            pos += len(line)
            if in_fence or stripped != "" or not line.endswith("\n"):
                continue
            # 空行 → 候选边界；后面必须还有内容且不是列表/缩进延续
            rest = self._pending[pos:]
            if not rest.strip():
                continue
            nxt = rest.lstrip("\n")
            if nxt.startswith(_CONTINUATION_PREFIXES) or _looks_like_ordered_item(nxt):
                continue
            boundary = pos
        if boundary > 0:
            done = self._pending[:boundary]
            self._pending = self._pending[boundary:]
            if done.strip():
                console.print(Markdown(done))

    def _end_reply(self) -> None:
        if self._mode != "reply":
            return
        self._stop_live()
        if self._pending.strip():
            console.print(Markdown(self._pending))
        self._pending = ""
        self._mode = ""

    # ---- 工具调用 / 结束 ----
    def on_tool_calls(self) -> None:
        self._stop_status()
        self._end_thinking()
        self._end_reply()

    def finish(self) -> None:
        self._stop_status()
        self._end_thinking()
        self._end_reply()
        self._stop_live()

    def abort(self) -> None:
        """异常/中断时保证 Live 与 spinner 都关掉。"""
        self._stop_status()
        self._stop_live()
        self._mode = ""
        self._pending = ""


def _looks_like_ordered_item(text: str) -> bool:
    head = text[:6]
    digits = ""
    for ch in head:
        if ch.isdigit():
            digits += ch
        else:
            return bool(digits) and ch in ".)" and text[len(digits) + 1 : len(digits) + 2] == " "
    return False


# ========================================================================
#  工具调用 / 结果
# ========================================================================

def tool_summary(name: str, args: dict) -> str:
    """一行概括「模型准备调用哪个工具」。"""
    if name == "read_file":
        paths = args.get("paths")
        if isinstance(paths, list) and paths:
            return f"{len(paths)} 个文件  {', '.join(str(p) for p in paths)}"
        s = str(args.get("path", ""))
        if args.get("start_line") or args.get("end_line"):
            s += f"  L{args.get('start_line', '?')}-{args.get('end_line', '?')}"
        return s
    if name == "write_file":
        files = args.get("files")
        if isinstance(files, list) and files:
            paths = ", ".join(str(f.get("path", "?")) for f in files if isinstance(f, dict))
            return f"{len(files)} 个文件  {paths}"
        return str(args.get("path", ""))
    if name == "edit_file":
        edits = args.get("edits")
        if isinstance(edits, list) and edits:
            paths = ", ".join(dict.fromkeys(str(e.get("path") or args.get("path") or "?") for e in edits if isinstance(e, dict)))
            return f"{len(edits)} 处修改  {paths}"
        return str(args.get("path", ""))
    if name == "edit_lines":
        mode = args.get("mode", "?")
        rng = f"L{args.get('start_line')}"
        if args.get("end_line") is not None and mode != "insert":
            rng += f"-{args.get('end_line')}"
        return f"{args.get('path', '')}  {mode} {rng}"
    if name == "delete_path":
        paths = args.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        return f"{len(paths)} 项  {', '.join(str(p) for p in paths)}"
    if name == "move_file":
        return f"{args.get('src')} → {args.get('dest')}"
    if name == "run_command":
        return str(args.get("command", ""))[:100]
    if name == "process":
        s = str(args.get("action", ""))
        if args.get("pid") is not None:
            s += f"  pid={args.get('pid')}"
        return s
    if name in {"list_dir", "check_syntax", "check_webpage"}:
        if name == "check_webpage" and args.get("url"):
            return str(args.get("url", ""))
        return str(args.get("path", ""))
    if name == "glob_search":
        return str(args.get("pattern", ""))
    if name == "grep_search":
        return f"{args.get('pattern', '')} @ {args.get('path', '')}"
    if name == "web_search":
        return str(args.get("query", ""))[:80]
    if name == "fetch_url":
        return str(args.get("url", ""))[:80]
    if name == "todo_write":
        items = args.get("todos") or []
        mode = "merge" if args.get("merge", True) else "replace"
        s = f"{len(items)} 项  ({mode})"
        for it in items:
            if isinstance(it, dict) and it.get("status") == "in_progress":
                s += f"  → {it.get('content') or it.get('id')}"
                break
        return s
    return ""


def show_tool_call(name: str, args: dict) -> None:
    summary = tool_summary(name, args)
    line = Text("● ", style="blue") + Text(name, style="bold blue")
    if summary:
        line += Text("  " + summary[:110], style="blue")
    console.print(line)
    if name == "run_command" and args.get("cwd"):
        console.print(f"  [dim]cwd: {args['cwd']}[/]")


def show_tool_result(result: str, name: str = "") -> None:
    """折叠显示工具结果：默认前几行 + 「… 还有 N 行」；/verbose 全量。"""
    ok = not (result.startswith("ERROR") or result.startswith("FAIL") or _has_nonzero_exit(result))
    style = "yellow" if result.startswith("SKIPPED:") else "green" if ok else "red"
    lines = result.rstrip().splitlines() or ["(empty)"]
    limit = len(lines) if config.VERBOSE else (_RESULT_LINES_COMMAND if name == "run_command" else _RESULT_LINES_DEFAULT)
    shown = lines[:limit]
    width = max(20, console.width - 8)
    for i, ln in enumerate(shown):
        prefix = "  ⎿  " if i == 0 else "     "
        text = ln if len(ln) <= width else ln[: width - 1] + "…"
        console.print(Text(prefix, style="dim") + Text(text, style=style))
    if len(lines) > limit:
        console.print(f"     [dim]… 还有 {len(lines) - limit} 行（/verbose 查看全部）[/]")


def _has_nonzero_exit(result: str) -> bool:
    for ln in result.splitlines()[:3]:
        if ln.startswith("exit=") and ln[5:].strip() not in {"0", ""}:
            return True
    return False


def show_note(msg: str) -> None:
    console.print(Text("  ⎿  ", style="dim") + Text(msg, style="dim cyan"))


# ========================================================================
#  diff 预览（确认前）
# ========================================================================

def _render_diff(old: str, new: str, path: str) -> Text:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=path,
        tofile=path,
        n=3,
    )
    lines = [ln.rstrip("\n") for ln in diff]
    out = Text()
    limit = len(lines) if config.VERBOSE else _DIFF_MAX_LINES
    for ln in lines[:limit]:
        if ln.startswith("+++") or ln.startswith("---"):
            style = "bold"
        elif ln.startswith("@@"):
            style = "cyan"
        elif ln.startswith("+"):
            style = "green"
        elif ln.startswith("-"):
            style = "red"
        else:
            style = "dim"
        out.append("  " + ln + "\n", style=style)
    if len(lines) > limit:
        out.append(f"  … diff 还有 {len(lines) - limit} 行（/verbose 查看全部）\n", style="dim")
    return out


def _render_new_file(path: str, content: str) -> Text:
    lines = content.splitlines()
    out = Text()
    out.append(f"  + 新文件 {path}  ({len(lines)} 行, {len(content)} 字符)\n", style="bold green")
    limit = len(lines) if config.VERBOSE else _NEW_FILE_PREVIEW_LINES
    for ln in lines[:limit]:
        out.append("  + " + ln + "\n", style="green")
    if len(lines) > limit:
        out.append(f"  … 还有 {len(lines) - limit} 行（/verbose 查看全部）\n", style="dim")
    return out


def show_change_preview(name: str, args: dict) -> bool:
    """写/改文件前展示变化；返回是否至少有一个可执行的变更。"""
    from agent.tools import preview_edit_file, preview_edit_lines

    try:
        if name == "edit_file":
            previews = preview_edit_file(args)
            valid = False
            for path, old, new, err in previews:
                if err:
                    console.print(f"  [red]{err}[/]")
                else:
                    valid = True
                    console.print(_render_diff(old, new, path))
            return valid
        elif name == "edit_lines":
            path, old, new, err = preview_edit_lines(args)
            if err:
                console.print(f"  [red]{err}[/]")
                return False
            else:
                console.print(_render_diff(old, new, path))
                return True
        elif name == "write_file":
            items = args.get("files") if isinstance(args.get("files"), list) else None
            if not items:
                items = [{"path": args.get("path"), "content": args.get("content", "")}]
            valid = False
            for f in items:
                if not isinstance(f, dict) or not f.get("path"):
                    continue
                valid = True
                p = resolve_path(str(f["path"]))
                content = str(f.get("content") or "")
                if p.is_file():
                    old = p.read_text(encoding="utf-8", errors="replace")
                    if old == content:
                        console.print(f"  [dim]{p}: 内容未变化[/]")
                    else:
                        console.print(_render_diff(old, content, str(p)))
                else:
                    console.print(_render_new_file(str(p), content))
            return valid
    except Exception as e:  # 预览失败不阻塞确认
        console.print(f"  [dim]（无法生成预览：{type(e).__name__}: {e}）[/]")
    return True


# ========================================================================
#  统计行 / 上下文
# ========================================================================

def ctx_bar(used: int, total: int, width: int = 20) -> str:
    ratio = min(1.0, used / total) if total else 0.0
    filled = int(round(ratio * width))
    color = "green" if ratio < 0.6 else "yellow" if ratio < 0.85 else "red"
    return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/] [{color}]{ratio * 100:.0f}%[/] [dim]{_fmt_k(used)}/{_fmt_k(total)}[/]"


def show_turn_stats(elapsed: float, stats: dict, tools: int, ctx_used: int, ctx_total: int) -> None:
    parts = [f"⏱ {elapsed:.1f}s"]
    tps = stats.get("predicted_per_second")
    if tps:
        parts.append(f"{tps:.0f} tok/s")
    prompt = stats.get("prompt_tokens") or stats.get("prompt_n")
    if prompt is not None or stats.get("predicted_n") is not None:
        parts.append(f"提示 {_fmt_k(prompt or 0)} / 生成 {_fmt_k(stats.get('predicted_n') or 0)}")
    parts.append(f"{tools} tools")
    if ctx_total:
        ratio = ctx_used / ctx_total * 100
        parts.append(f"ctx {ratio:.0f}%")
    console.print("[dim]" + " · ".join(parts) + "[/]")


def show_banner(cwd: str) -> None:
    """启动横幅：模型 · 上下文 · 工作目录 · 常用命令。"""
    from rich.panel import Panel

    model = config.MODEL_LABEL or "(未知模型)"
    n_ctx = config.n_ctx()
    lines = Text()
    lines.append("✻ Coder Agent\n", style="bold green")
    lines.append("本地终端编程助手，直接读文件、改代码、跑命令\n\n", style="dim")
    lines.append("模型  ", style="dim")
    lines.append(f"{model}", style="cyan")
    lines.append(f"  ctx {_fmt_k(n_ctx)}", style="dim")
    if config.MODEL_PARAMS_B:
        lines.append(f"  {config.MODEL_PARAMS_B:g}B", style="dim")
    lines.append("\n网页  ", style="dim")
    lines.append(f"http://{config.HOST}:{config.PORT}", style="blue underline")
    lines.append("  (llama-server 自带聊天界面)", style="dim")
    lines.append("\n目录  ", style="dim")
    lines.append(cwd, style="blue")
    lines.append("\n\n")
    lines.append("/help 命令 · /model 换模型 · /think off 关思考 · /resume 恢复 · /exit 退出", style="dim")
    console.print(Panel(lines, border_style="grey50", padding=(0, 1), expand=False))
    console.print()
