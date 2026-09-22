"""终端交互：ANSI 颜色（tools.py 里的小提示用）、按键读取、确认菜单、prompt_toolkit 输入。

画面渲染（markdown / diff / 工具结果）在 agent.render；这里只管「读用户按键」。
会话开关见 agent.config（AUTO_APPROVE*）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agent import config
from agent.config import CONFIRM_TOOLS, SLASH_MENU, is_safe_readonly_command


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    PROMPT = "\033[38;5;246m"
    USER_FG = "\033[38;5;255m"
    SEP = "\033[38;5;244m"
    SPINNER = "\033[38;5;174m"
    SPINNER_LABEL = "\033[38;5;216m"
    THINK_ICON = "\033[38;5;176m"
    THINK_TEXT = "\033[38;5;245m\033[3m"
    REPLY_ICON = "\033[38;5;114m"
    REPLY = "\033[38;5;252m"
    TOOL_ICON = "\033[38;5;75m"
    TOOL_OK = "\033[38;5;114m"
    TOOL_ERR = "\033[38;5;203m"
    STATUS = "\033[38;5;246m"
    TEAL = "\033[38;5;44m"
    PATH = "\033[38;5;75m"
    ERR = "\033[91m"


def enable_ansi() -> None:
    """在 Windows 控制台开启 ANSI 颜色转义。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def paint(text: str, *codes: str) -> str:
    """给文本包上 ANSI 颜色码。"""
    return f"{''.join(codes)}{text}{C.RESET}"


def flush_input_buffer() -> None:
    """清掉残留按键，避免上一次 Enter 直接把确认菜单秒过。"""
    if os.name == "nt":
        import msvcrt

        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0") and msvcrt.kbhit():
                msvcrt.getwch()
        return
    try:
        import select

        while select.select([sys.stdin], [], [], 0)[0]:
            if not sys.stdin.read(1):
                break
    except Exception:
        pass


# ========================================================================
#  单键读取（确认菜单用）
# ========================================================================

TOOL_SKIPPED_MESSAGE = (
    "SKIPPED: 用户选择暂不处理此操作。不要再次提出相同修改；"
    "继续检查其它问题，或总结已发现的问题后结束。"
)


def _read_key() -> str:
    """up / down / enter / esc / 1 / 2 / 3 / other"""
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            return {"H": "up", "P": "down"}.get(ch2, "other")
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                rest = sys.stdin.read(2)
                return {"[A": "up", "[B": "down"}.get(rest, "esc")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x1b":
        return "esc"
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch in ("1", "2", "3"):
        return ch
    if ch.lower() == "q":
        return "esc"
    return "other"


# ========================================================================
#  工具确认
# ========================================================================

def ask_tool_approval(name: str, args: dict) -> tuple[bool, str]:
    """写文件/跑命令前确认。返回 (是否允许, 拒绝原因)。"""
    from agent.render import console, show_change_preview, show_note

    if name not in CONFIRM_TOOLS:
        return True, ""
    if name == "run_command" and is_safe_readonly_command(str(args.get("command", ""))):
        show_note("只读命令，自动放行")
        return True, ""
    if name == "process" and str(args.get("action", "")).lower() in {"list", "read"}:
        return True, ""
    if config.AUTO_APPROVE_ALWAYS or config.AUTO_APPROVE:
        show_note("自动执行中")
        return True, ""

    if name in {"write_file", "edit_file", "edit_lines"}:
        if not show_change_preview(name, args):
            show_note("编辑参数无效，跳过确认并将错误返回给模型")
            return True, ""

    options = [
        ("执行", "仅本次"),
        ("自动执行", "本轮任务内不再询问"),
        ("暂不处理", "跳过此操作，继续检查或总结"),
    ]
    idx = 0

    flush_input_buffer()
    console.print()
    console.print("  [bold yellow]需要确认此操作[/]  [dim]↑↓ 选择  Enter 确认  Esc 拒绝[/]")

    def render() -> None:
        for i, (title, hint) in enumerate(options):
            if i == idx:
                sys.stdout.write(paint("  ❯ ", C.TEAL) + paint(title, C.BOLD, C.TEAL) + paint(f"  — {hint}", C.DIM, C.STATUS))
            else:
                sys.stdout.write("    " + paint(title, C.USER_FG) + paint(f"  — {hint}", C.DIM, C.STATUS))
            sys.stdout.write(" " * 12 + "\n")
        sys.stdout.flush()

    render()
    try:
        while True:
            key = _read_key()
            if key == "up":
                idx = (idx - 1) % len(options)
            elif key == "down":
                idx = (idx + 1) % len(options)
            elif key == "1":
                idx, key = 0, "enter"
            elif key == "2":
                idx, key = 1, "enter"
            elif key == "3":
                idx, key = 2, "enter"
            elif key == "esc":
                flush_input_buffer()
                try:
                    reason = console.input("  [dim]拒绝原因（可留空，会告诉模型）: [/]").strip()
                except (EOFError, KeyboardInterrupt):
                    reason = ""
                console.print("  [red]✗ 已拒绝[/]" + (f"[dim]：{reason}[/]" if reason else ""))
                return False, reason
            if key == "enter":
                if idx == 2:
                    console.print("  [yellow]○ 已暂不处理此操作[/]")
                    flush_input_buffer()
                    return False, TOOL_SKIPPED_MESSAGE
                if idx == 1:
                    config.AUTO_APPROVE = True
                    console.print("  [cyan]✓ 本轮自动执行（下一条消息会重新询问）[/]")
                else:
                    console.print("  [cyan]✓ 已确认执行[/]")
                flush_input_buffer()
                return True, ""
            if key in {"up", "down"}:
                sys.stdout.write(f"\033[{len(options)}A")
                render()
    except KeyboardInterrupt:
        # 交给上层：取消整轮任务，而不是仅拒绝当前工具
        print()
        raise


# ========================================================================
#  用户输入（prompt_toolkit：历史 / 多行 / 斜杠补全 / 底部状态栏）
# ========================================================================

_session = None
_toolbar_provider = None


def set_toolbar_provider(fn) -> None:
    """main 注入一个返回状态栏文本的函数（模型 · ctx · 模式 · cwd）。"""
    global _toolbar_provider
    _toolbar_provider = fn


def _build_session():
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/") or " " in text or "\n" in text:
                return
            low = text.lower()
            for cmd, hint in SLASH_MENU:
                if cmd.startswith(low):
                    yield Completion(cmd, start_position=-len(text), display=cmd, display_meta=hint)

    kb = KeyBindings()

    def _in_fence(text: str) -> bool:
        return text.lstrip().startswith("```") and text.count("```") < 2

    @kb.add("enter", filter=Condition(lambda: True))
    def _enter(event) -> None:
        buf = event.current_buffer
        if buf.complete_state and buf.complete_state.current_completion:
            buf.apply_completion(buf.complete_state.current_completion)
            return
        if _in_fence(buf.text):
            buf.insert_text("\n")
            return
        buf.validate_and_handle()

    @kb.add("escape", "enter")  # Alt+Enter / Esc 后 Enter
    @kb.add("c-j")
    def _newline(event) -> None:
        event.current_buffer.insert_text("\n")

    def toolbar():
        if _toolbar_provider is None:
            return ""
        try:
            return _toolbar_provider()
        except Exception:
            return ""

    style = Style.from_dict(
        {
            "prompt": "#8a8a8a",
            "bottom-toolbar": "noreverse #8a8a8a bg:default",
            "completion-menu.completion": "bg:#303030 #d0d0d0",
            "completion-menu.completion.current": "bg:#005f5f #ffffff",
            "completion-menu.meta.completion": "bg:#303030 #8a8a8a",
            "completion-menu.meta.completion.current": "bg:#005f5f #d0d0d0",
        }
    )
    return PromptSession(
        history=FileHistory(str(config.HISTORY_FILE)),
        completer=SlashCompleter(),
        complete_while_typing=True,
        key_bindings=kb,
        multiline=True,
        prompt_continuation=lambda width, line_number, is_soft_wrap: "  ",
        bottom_toolbar=toolbar,
        style=style,
        enable_history_search=False,
    )


def read_input() -> str:
    """读一条用户输入（可多行：以 ``` 开头进入多行，再以 ``` 结束；或 Alt+Enter 换行）。"""
    global _session
    if not sys.stdin.isatty():
        sys.stdout.write("❯ ")
        sys.stdout.flush()
        return input().strip()
    if _session is None:
        _session = _build_session()
    from agent.render import console

    console.rule(style="grey35")
    text = _session.prompt([("class:prompt", "❯ ")])
    text = text.strip()
    # ```...``` 包裹的多行输入：去掉外层围栏
    if text.startswith("```") and text.endswith("```") and text.count("```") == 2:
        inner = text[3:-3]
        text = inner.strip("\n").strip()
    return text
