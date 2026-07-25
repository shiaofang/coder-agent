"""终端界面：颜色、横幅、确认菜单、流式输出、斜杠命令输入。

用 ANSI 转义码给终端上色；写文件/命令前用 ↑↓ 菜单让用户确认。
会话开关见 agent.config（AUTO_APPROVE*）。
"""

from __future__ import annotations

import os
import re
import sys

from agent import config
from agent.config import CONFIRM_TOOLS, SLASH_MENU

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
    THINK_TEXT = "\033[38;5;238m\033[2m\033[3m"
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
    """在 Windows 控制台开启 ANSI 颜色转义，否则 paint() 的颜色码会原样显示。"""
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
    """给文本包上 ANSI 颜色码，打印彩色终端文字。"""
    return f"{''.join(codes)}{text}{C.RESET}"

def sep(width: int | None = None) -> None:
    """打印一条横线分隔符。"""
    cols = width or min(term_cols(), 72)
    print(paint("─" * cols, C.SEP))

def term_cols() -> int:
    """获取终端宽度（列数），失败则返回 72。"""
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 72

# 清掉键盘缓冲区，避免上一次回车直接把确认菜单秒过

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

def print_banner() -> None:
    """
    启动时打印一段简单的自我介绍横幅（参考 Claude Code / Codex CLI 等终端产品的欢迎框），
    避免刚进终端时一片空白。
    """
    width = min(term_cols(), 72)
    inner = width - 4  # 边框 "│ " 与 " │" 共占 4 格

    def line(painted: str = "") -> str:
        pad = max(0, inner - _visible_cols(painted))
        return paint("│ ", C.SEP) + painted + " " * pad + paint(" │", C.SEP)

    # 每行文字都先按当前宽度裁剪，避免窄终端下把方框撑破
    title = _truncate_display("✻ Coder Agent", inner)
    subtitle = _truncate_display("本地终端编程助手，直接读文件、改代码、跑命令、查资料", inner)
    tip1 = _truncate_display('说说想做什么，例如："帮我在这个目录创建一个 vue3 项目"', inner)
    tip2_full = "/ 查看命令  ·  /auto 自动执行  ·  /exit 退出"
    tip2 = _truncate_display(tip2_full, inner)

    print()
    print(paint("╭" + "─" * (width - 2) + "╮", C.SEP))
    print(line(paint(title, C.BOLD, C.REPLY_ICON)))
    print(line(paint(subtitle, C.DIM, C.STATUS)))
    print(line())
    print(line(paint(tip1, C.DIM, C.STATUS)))
    if tip2 == tip2_full:
        # 宽度够时才分色高亮命令，否则退化为单色裁剪文本
        print(
            line(
                paint("/", C.TEAL)
                + paint(" 查看命令  ·  ", C.DIM, C.STATUS)
                + paint("/auto", C.TEAL)
                + paint(" 自动执行  ·  ", C.DIM, C.STATUS)
                + paint("/exit", C.TEAL)
                + paint(" 退出", C.DIM, C.STATUS)
            )
        )
    else:
        print(line(paint(tip2, C.DIM, C.STATUS)))
    print(paint("╰" + "─" * (width - 2) + "╯", C.SEP))
    print()

def _status_text_width(text: str) -> int:
    """文本显示宽度（中文等宽字符按 2 算）。"""
    w = 0
    for ch in text:
        o = ord(ch)
        if o <= 0x7F:
            w += 1
        elif 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F:
            w += 2
        else:
            w += 1
    return w

def _truncate_display(text: str, max_w: int) -> str:
    """按显示宽度裁剪文本，超出部分用省略号代替。"""
    if max_w <= 0:
        return ""
    if _status_text_width(text) <= max_w:
        return text
    out = text
    while out and _status_text_width(out + "…") > max_w:
        out = out[:-1]
    return (out + "…") if out else "…"

def _read_input_key() -> tuple[str, str]:
    """
    读一个按键。返回 (kind, value)：
      up/down/enter/esc/backspace/tab  → value 为空
      char → value 为字符
    """
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            if ch2 == "H":
                return ("up", "")
            if ch2 == "P":
                return ("down", "")
            if ch2 == "K":  # left — ignore
                return ("other", "")
            if ch2 == "M":  # right — ignore
                return ("other", "")
            return ("other", "")
        if ch in ("\r", "\n"):
            return ("enter", "")
        if ch == "\x1b":
            return ("esc", "")
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\x08", "\x7f"):
            return ("backspace", "")
        if ch == "\t":
            return ("tab", "")
        if ch >= " " and ch != "\x7f":
            return ("char", ch)
        return ("other", "")

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            rest = sys.stdin.read(2)
            if rest == "[A":
                return ("up", "")
            if rest == "[B":
                return ("down", "")
            return ("esc", "")
        if ch in ("\r", "\n"):
            return ("enter", "")
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\x7f", "\x08"):
            return ("backspace", "")
        if ch == "\t":
            return ("tab", "")
        if ch >= " ":
            return ("char", ch)
        return ("other", "")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def _read_key() -> str:
    """工具确认菜单用：up / down / enter / esc / 1 / 2 / other"""
    kind, val = _read_input_key()
    if kind == "char" and val in ("1", "2"):
        return val
    if kind == "char" and val.lower() == "q":
        return "esc"
    if kind in {"up", "down", "enter", "esc"}:
        return kind
    return "other"

def ask_tool_approval(name: str, args: dict) -> bool:
    """↑↓ 选择，Enter 确认。返回 True 表示允许执行。"""
    if name not in CONFIRM_TOOLS:
        return True
    if config.AUTO_APPROVE_ALWAYS or config.AUTO_APPROVE:
        print(paint("  ⎿  ", C.DIM) + paint("自动执行中", C.DIM, C.TEAL))
        return True

    options = [
        ("执行", "仅本次"),
        ("自动执行", "本轮任务内不再询问"),
    ]
    idx = 0

    flush_input_buffer()
    print()
    print(paint("  需要确认此操作", C.BOLD, C.SPINNER_LABEL))
    print(paint("  ↑↓ 选择  Enter 确认  Esc 取消", C.DIM, C.STATUS))

    def render() -> None:
        for i, (title, hint) in enumerate(options):
            if i == idx:
                line = (
                    paint("  ❯ ", C.TEAL)
                    + paint(title, C.BOLD, C.TEAL)
                    + paint(f"  — {hint}", C.DIM, C.STATUS)
                )
            else:
                line = (
                    paint("    ", C.DIM)
                    + paint(title, C.USER_FG)
                    + paint(f"  — {hint}", C.DIM, C.STATUS)
                )
            sys.stdout.write(line + " " * 12 + "\n")
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
                idx = 0
                key = "enter"
            elif key == "2":
                idx = 1
                key = "enter"
            elif key == "esc":
                return False
            if key == "enter":
                if idx == 1:
                    config.AUTO_APPROVE = True
                    print(paint("  ✓ 本轮自动执行（下一条消息会重新询问）", C.TEAL))
                else:
                    print(paint("  ✓ 已确认执行", C.TEAL))
                flush_input_buffer()
                return True
            if key in {"up", "down"}:
                sys.stdout.write(f"\033[{len(options)}A")
                render()
    except KeyboardInterrupt:
        # 交给上层：取消整轮任务，而不是仅拒绝当前工具
        print()
        raise

def show_tool_call(name: str, args: dict) -> None:
    """在终端漂亮地显示「模型准备调用哪个工具」。"""
    summary = ""
    if name in {
        "read_file",
        "write_file",
        "edit_file",
        "replace_lines",
        "insert_lines",
        "delete_lines",
        "delete_file",
        "mkdir",
        "list_dir",
    } and "path" in args:
        summary = str(args["path"])
        if name == "read_file" and (args.get("start_line") or args.get("end_line")):
            summary += f"  L{args.get('start_line', '?')}-{args.get('end_line', '?')}"
        elif name == "replace_lines" or name == "delete_lines":
            summary += f"  L{args.get('start_line')}-{args.get('end_line')}"
        elif name == "insert_lines":
            summary += f"  after L{args.get('after_line')}"
    elif name == "move_file":
        summary = f"{args.get('src')} → {args.get('dest')}"
    elif name == "run_command" and "command" in args:
        summary = str(args["command"])[:80]
    elif name == "glob_search":
        summary = str(args.get("pattern", ""))
    elif name == "grep_search":
        summary = f"{args.get('pattern', '')} @ {args.get('path', '')}"
    elif name == "web_search":
        summary = str(args.get("query", ""))[:80]
    elif name == "fetch_url":
        summary = str(args.get("url", ""))[:80]
    label = f"{name}" + (f"  {summary}" if summary else "")
    print(paint("●", C.TOOL_ICON) + " " + paint(label, C.BOLD, C.TOOL_ICON))
    if name == "write_file" and "content" in args:
        preview = str(args["content"]).replace("\n", "\\n")
        if len(preview) > 80:
            preview = preview[:77] + "..."
        print(paint("  content: ", C.DIM) + paint(preview, C.DIM, C.STATUS))
    if name == "edit_file" and "old_text" in args:
        old = str(args["old_text"]).replace("\n", "\\n")
        new = str(args.get("new_text", "")).replace("\n", "\\n")
        if len(old) > 50:
            old = old[:47] + "..."
        if len(new) > 50:
            new = new[:47] + "..."
        print(paint("  ", C.DIM) + paint(f"{old}  →  {new}", C.DIM, C.STATUS))
    if name == "run_command" and args.get("cwd"):
        print(paint("  cwd: ", C.DIM) + paint(str(args["cwd"]), C.DIM, C.STATUS))

def show_tool_result(result: str) -> None:
    """在终端显示工具执行结果摘要（成功绿/失败红）。"""
    ok = not (
        result.startswith("ERROR")
        or result.startswith("FAIL")
        or re.search(r"(?m)^exit=[1-9]", result) is not None
    )
    color = C.TOOL_OK if ok else C.TOOL_ERR
    preview = result.replace("\n", " | ")
    if len(preview) > 100:
        preview = preview[:97] + "..."
    print(paint("  ⎿  ", C.DIM) + paint(preview, color))

def write_stream(text: str, color: str, indent: str = "") -> None:
    """流式逐字打印模型输出（思考过程或最终回复）。"""
    for ch in text:
        if ch == "\n":
            sys.stdout.write(C.RESET + "\n" + indent + color)
        else:
            sys.stdout.write(color + ch)
    sys.stdout.flush()

def _slash_matches(prefix: str) -> list[tuple[str, str]]:
    """按已输入前缀过滤斜杠命令（前缀不含大小写敏感）。"""
    p = prefix.lower()
    return [(cmd, hint) for cmd, hint in SLASH_MENU if cmd.lower().startswith(p)]


def _visible_cols(text: str) -> int:
    """粗算终端显示宽度（用于把光标移回输入末尾）。"""
    plain = re.sub(r"\033\[[0-9;]*m", "", text)
    w = 0
    for ch in plain:
        o = ord(ch)
        if o <= 0x7F:
            w += 1
        elif 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F:
            w += 2
        else:
            w += 1
    return w

def read_input() -> str:
    """
    打印提示符并读一行用户输入（返回 strip 后的字符串）。
    输入 / 时在下方「悬浮」命令列表，光标始终停在输入行；↑↓ 选择，Tab/Enter 确认，Esc 关闭。
    """
    sep()

    if not sys.stdin.isatty():
        sys.stdout.write(paint("❯", C.PROMPT) + " ")
        sys.stdout.flush()
        try:
            return input().strip()
        except (EOFError, KeyboardInterrupt):
            raise

    buf = ""
    menu_idx = 0
    menu_suppressed = False
    menu_count = 0  # 当前悬浮菜单行数

    def matches() -> list[tuple[str, str]]:
        if not buf.startswith("/"):
            return []
        return _slash_matches(buf)

    def clear_float() -> None:
        """清掉输入行及下方悬浮菜单，光标回到行首。"""
        nonlocal menu_count
        sys.stdout.write("\r\033[J")
        sys.stdout.flush()
        menu_count = 0

    def render() -> None:
        """重绘输入行，菜单画在下方后把光标移回输入末尾。"""
        nonlocal menu_idx, menu_count
        clear_float()

        items = matches()
        show_menu = bool(items) and not menu_suppressed
        if show_menu:
            menu_idx %= len(items)

        prompt = paint("❯", C.PROMPT) + " " + paint(buf, C.USER_FG)
        sys.stdout.write(prompt)

        if show_menu:
            for i, (cmd, hint) in enumerate(items):
                if i == menu_idx:
                    line = (
                        paint("  ❯ ", C.TEAL)
                        + paint(cmd, C.BOLD, C.TEAL)
                        + paint(f"  {hint}", C.DIM, C.STATUS)
                    )
                else:
                    line = (
                        paint("    ", C.DIM)
                        + paint(cmd, C.STATUS)
                        + paint(f"  {hint}", C.DIM, C.STATUS)
                    )
                sys.stdout.write("\n\033[2K" + line)
            menu_count = len(items)
            # 回到输入行，光标落在 buf 末尾（❯ + 空格 + buf）
            col = 1 + _visible_cols("❯ ") + _visible_cols(buf)
            sys.stdout.write(f"\033[{menu_count}A\033[{col}G")
        else:
            menu_count = 0

        sys.stdout.flush()

    def finish(text: str) -> str:
        """提交：清掉悬浮层，留下一行最终输入。"""
        clear_float()
        sys.stdout.write(paint("❯", C.PROMPT) + " " + paint(text, C.USER_FG) + "\n")
        sys.stdout.flush()
        return text

    render()
    try:
        while True:
            kind, val = _read_input_key()
            items = matches()
            show_menu = bool(items) and not menu_suppressed

            if kind == "up" and show_menu:
                menu_idx = (menu_idx - 1) % len(items)
                render()
                continue
            if kind == "down" and show_menu:
                menu_idx = (menu_idx + 1) % len(items)
                render()
                continue
            if kind == "tab" and show_menu:
                buf = items[menu_idx][0]
                menu_idx = 0
                menu_suppressed = False
                render()
                continue
            if kind == "esc":
                if show_menu:
                    menu_suppressed = True
                    render()
                    continue
                return finish(buf.strip())
            if kind == "enter":
                if show_menu:
                    return finish(items[menu_idx][0])
                return finish(buf.strip())
            if kind == "backspace":
                if buf:
                    buf = buf[:-1]
                    menu_idx = 0
                    menu_suppressed = False
                render()
                continue
            if kind == "char":
                if not buf and val.isspace():
                    continue
                buf += val
                menu_idx = 0
                menu_suppressed = False
                render()
                continue
    except KeyboardInterrupt:
        clear_float()
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise

