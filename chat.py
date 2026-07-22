#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地终端 AI 编程助手（chat.py）
================================

【这个程序在干什么？】
  start.bat 会先启动 bin/llama-server.exe（本地大模型服务），
  再运行本文件。本文件是一个「终端聊天客户端 + 工具执行器」：

  1. 你在终端里打字（例如：帮我在某某目录创建一个 Vue 项目）
  2. 本程序把对话发给本地模型（http://127.0.0.1:8080）
  3. 模型可以「调用工具」（读文件、改文件、跑命令、搜网页…）
  4. 本程序真正执行这些工具，把结果再发给模型
  5. 模型根据结果继续思考，直到给出最终中文回复

【为什么没有 LangChain / @tool？】
  这是故意手写的「OpenAI 兼容 tool calling」：
  - TOOLS 列表 = 告诉模型「你有哪些工具、参数长什么样」
  - execute_tool() = 模型说完要调用后，真正去执行
  不依赖第三方 Agent 框架，装好 Python 就能跑。

【怎么读这个文件？（按功能分区，从上往下）】
  第 1 区  配置常量 ………… 地址、命令、安全开关
  第 2 区  系统提示词 ……… 教模型怎么当编程助手
  第 3 区  工具声明 TOOLS … 给模型看的工具说明书（JSON Schema）
  第 4 区  终端界面 ………… 颜色、横幅、确认菜单
  第 5 区  路径辅助 ………… Windows 路径解析
  第 6 区  工具实现 ………… tool_xxx 真正干活的函数
  第 7 区  工具调度 ………… execute_tool 按名字分发
  第 8 区  模型 API ………… 跟 llama-server 通信
  第 9 区  Agent 循环 ……… 多轮「思考 → 用工具 → 再思考」
  第10 区  主程序入口 ……… main() / if __name__ == "__main__"

【推荐阅读顺序（初学者）】
  main() → run_agent_turn() → chat_once() → execute_tool() → 任意一个 tool_xxx
"""

from __future__ import annotations

# 标准库导入（没有第三方依赖，如 requests / langchain）：
#   json / urllib  — 跟本地模型 HTTP 通信、解析 JSON
#   subprocess    — 执行 shell 命令
#   pathlib.Path  — 更方便地处理文件路径
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


# ========================================================================
#  第 1 区：配置常量

# ========================================================================
# 本地模型服务地址、斜杠命令、安全上限、写操作确认名单。
# 改 HOST/PORT 时，要保证和 start.bat 里启动 llama-server 的参数一致。
#
# 本地 llama-server 的地址（OpenAI 兼容 API）
HOST = "127.0.0.1"
PORT = 8080
BASE = f"http://{HOST}:{PORT}"

# 用户可输入的斜杠命令（不区分大小写，在 main 里处理）
EXIT_CMDS = {"/exit", "/quit", "/q", "exit", "quit"}
CLEAR_CMDS = {"/clear", "/reset", "/new"}
AUTO_CMDS = {"/auto"}
MANUAL_CMDS = {"/manual"}

# 安全与性能上限：
#   MAX_TOOL_ROUNDS     — 一轮用户任务里，最多允许「模型调工具」多少次，防止死循环
#   MAX_READ_CHARS      — 读文件返回给模型的最大字符数，避免上下文爆掉
#   MAX_REASONING_*     — 小模型「思考」阶段有时会重复啰嗦，用来检测并打断
MAX_TOOL_ROUNDS = 48
MAX_READ_CHARS = 80_000
MAX_REASONING_CHARS = 6000
REASONING_LOOP_NGRAM = 24
REASONING_LOOP_THRESHOLD = 4
MAX_REASONING_ABORTS = 3

# 写操作 / 跑命令前，需要用户在终端确认（读文件等安全操作直接放行）。
# AUTO_APPROVE        — 本轮任务内自动放行（选「自动执行」或下一轮会重置）
# AUTO_APPROVE_ALWAYS — 全局自动（用户输入 /auto），直到 /manual
CONFIRM_TOOLS = {
    "write_file",
    "edit_file",
    "replace_lines",
    "insert_lines",
    "delete_lines",
    "delete_file",
    "move_file",
    "run_command",
}
AUTO_APPROVE = False
AUTO_APPROVE_ALWAYS = False

# ========================================================================
#  第 2 区：系统提示词（教模型怎么工作）
# ========================================================================
# SYSTEM_PROMPT 会作为 messages 里 role=system 的内容发给模型。
#
SYSTEM_PROMPT = """你是编程助手，用工具直接改文件、跑命令。必须真干，禁止只给步骤；禁止声称无法访问文件系统。完成用简短中文总结，立刻停工具。

路径：用户给的绝对路径必须原样传给工具，禁止改成相对路径。

命令：禁止编造不存在的 CLI 参数。不确定先 --help 或查文档。脚手架失败/卡住 → 立刻 write_file 手写最小项目。禁止前台跑常驻服务（npm run dev 等）；用 run_command 即可（会自动后台）。

改代码：小改用 edit_file / replace_lines / insert_lines / delete_lines，禁止整文件重写。流程：read → 精确改 → 再验证。仅新建或大改才 write_file。

排错：先读报错指向的文件与行号，禁止地毯式瞎猜。同一错误 2 次未修好 → web_search 错误原文后再改。禁止无资料第 3 次同思路硬修。

验收：声称完成前必须跑构建/检查，总结写清命令与结果。
"""


def build_system_prompt() -> str:
    """组装发给模型的系统提示：通用规则 + 当前工作目录。"""
    cwd = str(Path.cwd())
    return (
        SYSTEM_PROMPT
        + f"\n\n【运行环境】\n当前工作目录（cwd）= {cwd}\n"
        + "相对路径会解析到上述 cwd。用户消息里的绝对路径请完整复制到工具参数。"
    )

# ========================================================================
#  第 3 区：工具声明 TOOLS（给模型看的「说明书」）

# ========================================================================
# 格式是 OpenAI function calling 约定，不是随便写的：
#   type / function / name / description / parameters(JSON Schema)
# 
# required: ["path"] 表示调用时必须带 path 参数。
# 注意：这里只是「声明」。真正执行在第 6、7 区的 tool_xxx / execute_tool。
# 新增工具时要改三处：TOOLS + tool_xxx 实现 + execute_tool 分支。
#
PATH_HINT = "优先使用用户给出的绝对路径（如 C:\\\\Users\\\\...），不要擅自改成相对路径"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取文本文件；默认带行号。可指定 start_line/end_line 只读片段，"
                "便于配合 replace_lines / delete_lines。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "start_line": {"type": "integer", "description": "起始行（1-based），可选"},
                    "end_line": {"type": "integer", "description": "结束行（含），可选"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "创建新文件或在必须整文件重写时覆盖写入。"
                "已有文件的小改动禁止用本工具，请用 edit_file / replace_lines / insert_lines / delete_lines。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "精确文本替换（改几个字符/一句话首选）。old_text→new_text；"
                "默认只替换第一处，replace_all=true 时替换全部。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "old_text": {"type": "string", "description": "要被替换的原文（需足够独特）"},
                    "new_text": {"type": "string", "description": "替换后的新文本，可为空表示删除该段"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有匹配，默认 false",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_lines",
            "description": "按行号替换一段代码（1-based，含首尾）。适合改连续几行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "start_line": {"type": "integer", "description": "起始行号（从1开始）"},
                    "end_line": {"type": "integer", "description": "结束行号（含）"},
                    "new_content": {"type": "string", "description": "替换后的内容（可多行）"},
                },
                "required": ["path", "start_line", "end_line", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_lines",
            "description": "在指定行后插入代码（after_line=0 表示文件开头）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "after_line": {"type": "integer", "description": "插入到该行之后，0=文件开头"},
                    "content": {"type": "string", "description": "要插入的文本"},
                },
                "required": ["path", "after_line", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_lines",
            "description": "按行号删除一段代码（1-based，含首尾）。挪代码时配合 insert_lines 使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除文件（不是删行）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "移动或重命名文件/目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": f"源路径。{PATH_HINT}"},
                    "dest": {"type": "string", "description": f"目标路径。{PATH_HINT}"},
                },
                "required": ["src", "dest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mkdir",
            "description": "创建目录（含中间目录）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"目录路径。{PATH_HINT}"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认当前目录"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "按 glob 模式查找文件，例如 **/*.js",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "root": {"type": "string", "description": "搜索根目录，默认当前目录"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "在文件中搜索文本（正则），返回 path:line:content",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "文件或目录"},
                    "glob": {"type": "string", "description": "可选，限制文件名如 *.py"},
                },
                "required": ["pattern", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在 shell 中执行命令。短命令前台等待；"
                "npm run dev / vite 等常驻服务会自动后台启动并返回启动日志与访问地址。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "工作目录，可选"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索（查报错、API、官方文档）。"
                "同一 build/lint 错误修两次仍失败时必须调用，查询应包含完整报错关键词。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，建议带上完整错误信息"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "抓取网页正文（文档/StackOverflow/GitHub issue），配合 web_search 使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http/https URL"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_datetime",
            "description": "获取当前本地日期时间",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ========================================================================
#  第 4 区：终端界面（颜色、输入、确认菜单）

# ========================================================================
# 用 ANSI 转义码给终端上色；写文件/命令前用 ↑↓ 菜单让用户确认。
#
# 颜色码集合：C.ERR 红色错误，C.TEAL 青色提示，等等

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

def print_banner(model_id: str) -> None:
    """打印启动横幅：模型名、快捷命令说明。"""
    print()
    print(paint(f"  {model_id}", C.STATUS))
    print(paint("  tools: edit/lines/files/grep/shell/web", C.TEAL))
    print(paint("  写文件/命令：↑↓ 选择  Enter 确认", C.STATUS))
    print(paint("  /clear 清空  /auto 全程自动  /manual 每次确认  /exit 退出", C.DIM, C.STATUS))
    print(paint("  Ctrl+C 取消当前任务；提示符下再按一次退出", C.DIM, C.STATUS))
    print()

def _read_key() -> str:
    """Return: up / down / enter / esc / 1 / 2 / other"""
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            if ch2 == "H":
                return "up"
            if ch2 == "P":
                return "down"
            return "other"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("1", "2"):
            return ch
        if ch.lower() == "q":
            return "esc"
        return "other"

    # POSIX fallback
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
                return "up"
            if rest == "[B":
                return "down"
            return "esc"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("1", "2"):
            return ch
        return "other"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def ask_tool_approval(name: str, args: dict) -> bool:
    """↑↓ 选择，Enter 确认。返回 True 表示允许执行。"""
    global AUTO_APPROVE, AUTO_APPROVE_ALWAYS
    if name not in CONFIRM_TOOLS:
        return True
    if AUTO_APPROVE_ALWAYS or AUTO_APPROVE:
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
                    AUTO_APPROVE = True
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

# ========================================================================
#  第 5 区：路径辅助

# ========================================================================
# 模型有时会把 C:\Users\...\Desktop\foo 错误缩短成 foo。
# resolve_path 尽量纠正；extract_abs_paths 从用户原话里抠出绝对路径提醒模型。
#

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

    # Recover common mistake: C:\Users\...\Desktop\tttt -> tttt
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

def extract_abs_paths(text: str) -> list[str]:
    """Find Windows absolute paths mentioned in user text."""
    # Stop before whitespace / Chinese punctuation so "C:\\a\\b，看下" works
    found = re.findall(
        r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n\s，。；、！？]+\\)*[^\\/:*?\"<>|\r\n\s，。；、！？]+",
        text,
    )
    # Deduplicate, keep order
    seen: set[str] = set()
    out: list[str] = []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

# ========================================================================
#  第 6 区：工具实现（模型调用后，这里真正干活）

# ========================================================================
# 函数名约定：tool_<工具名>。返回值是字符串，会作为 role=tool 的内容发回模型。
# 成功一般以 OK: 开头，失败以 ERROR: / FAIL 开头。
#
# ---------- 6.1 文件读写与编辑 ----------

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

def tool_grep_search(pattern: str, path: str, glob: str | None = None) -> str:
    """工具实现：在文件/目录中用正则搜索文本。"""
    p = resolve_path(path)
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
    else:
        return f"ERROR: path not found: {p}"

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

    urls = re.findall(r"https?://[\w\.-]+:\d+\S*", out)
    url_hint = f"\nurl={urls[0]}" if urls else "\nurl=(check log / default http://localhost:5173)"
    return (
        f"OK: started in background\n"
        f"pid={proc.pid}\ncwd={work}{url_hint}\n"
        f"log={log_path}\n"
        f"(dev server keeps running; do not wait for it to exit)\n"
        f"--- startup log ---\n{out}"
    )

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

def extract_error_fingerprint(text: str) -> str:
    """从命令输出里抽出简短错误指纹，用于判断是否反复同一报错。"""
    patterns = [
        r"ERROR:\s*[^\n]+",
        r"error during build:[^\n]*",
        r"SyntaxError:[^\n]+",
        r"TypeError:[^\n]+",
        r"Cannot read properties of undefined \(reading '[^']+'\)",
        r"Module not found:[^\n]*",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(0))[:160]
    for line in text.splitlines():
        if re.search(r"error|fail|exception", line, re.I):
            return line.strip()[:160]
    return ""

def tool_call_signature(name: str, args: dict) -> str:
    """把一次工具调用压成字符串签名，用来检测「完全相同的重复操作」。"""
    if name == "edit_file":
        return f"edit|{args.get('path')}|{args.get('old_text')}|{args.get('new_text')}"
    if name == "replace_lines":
        return (
            f"repl|{args.get('path')}|{args.get('start_line')}-"
            f"{args.get('end_line')}|{args.get('new_content')}"
        )
    if name == "run_command":
        cmd = str(args.get("command") or "")
        if "build" in cmd or "test" in cmd or "lint" in cmd:
            return f"check|{cmd}"
    if name == "web_search":
        return f"search|{args.get('query')}"
    return f"{name}|{json.dumps(args, ensure_ascii=False, sort_keys=True)[:200]}"

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
        if name == "edit_file":
            return tool_edit_file(
                args["path"],
                args["old_text"],
                args["new_text"],
                bool(args.get("replace_all", False)),
            )
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

# ========================================================================
#  第 8 区：与本地模型服务通信

# ========================================================================
# llama-server 提供 OpenAI 风格接口：
#   GET  /health              — 是否就绪
#   GET  /v1/models           — 当前加载的模型
#   POST /v1/chat/completions — 聊天（本程序用 stream=True 流式接收）
#

def request_json(method: str, path: str, body: dict | None = None, timeout: float = 600.0):
    """向本地 llama-server 发 HTTP 请求，解析返回的 JSON。"""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def wait_ready(retries: int = 120) -> bool:
    """轮询 /health，等模型服务就绪；带加载动画。"""
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    for i in range(retries):
        try:
            request_json("GET", "/health", timeout=2)
            sys.stdout.write("\r" + " " * 48 + "\r")
            sys.stdout.flush()
            return True
        except Exception:
            frame = frames[i % len(frames)]
            sys.stdout.write(
                f"\r{paint('✻', C.SPINNER)} {paint(frame + ' Loading model…', C.SPINNER_LABEL)}"
            )
            sys.stdout.flush()
            time.sleep(0.45)
    return False

# ========================================================================
#  第 9 区：Agent 循环（核心逻辑）

# ========================================================================
# chat_once  = 问模型一次（可能得到文字，也可能得到 tool_calls）
# run_agent_turn = 若有工具就执行，把结果塞回 messages，再问，直到最终回答
#

def _detect_reasoning_loop(
    text: str,
    ngram: int = REASONING_LOOP_NGRAM,
    threshold: int = REASONING_LOOP_THRESHOLD,
) -> bool:
    """Cheap n-gram repetition detector for degenerate 'thinking' loops."""
    if len(text) < ngram * threshold:
        return False
    counts: dict[str, int] = {}
    step = max(1, ngram // 2)
    for i in range(0, len(text) - ngram, step):
        gram = text[i : i + ngram]
        if not gram.strip():
            continue
        n = counts.get(gram, 0) + 1
        counts[gram] = n
        if n >= threshold:
            return True
    return False

def chat_once(messages: list[dict]) -> tuple[str, list[dict], str, bool]:
    """One model turn. Returns (content, tool_calls, reasoning, looped)."""
    # 发给本地模型的请求体：历史消息 + 工具说明书 + 流式输出
    payload = {
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": True,
        "temperature": 0.3,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_acc: dict[int, dict] = {}
    thinking = False
    replied = False
    started = False
    looped = False
    reasoning_len_at_last_check = 0

    sys.stdout.write(paint("✻", C.SPINNER) + " " + paint("Working…", C.SPINNER_LABEL))
    sys.stdout.flush()

    def clear_spinner() -> None:
        nonlocal started
        if not started:
            sys.stdout.write("\r" + " " * 28 + "\r")
            sys.stdout.flush()
            started = True

    with urllib.request.urlopen(req, timeout=600) as resp:
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue

            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            content = delta.get("content")
            tool_calls = delta.get("tool_calls")

            if reasoning:
                clear_spinner()
                if not thinking:
                    thinking = True
                    print(paint("∴", C.THINK_ICON) + " " + paint("Thinking…", C.DIM, C.SPINNER_LABEL))
                    sys.stdout.write("  " + C.THINK_TEXT)
                    sys.stdout.flush()
                write_stream(reasoning, C.THINK_TEXT, indent="  ")
                reasoning_parts.append(reasoning)

                total_reasoning_len = sum(len(r) for r in reasoning_parts)
                if total_reasoning_len > MAX_REASONING_CHARS:
                    looped = True
                elif total_reasoning_len - reasoning_len_at_last_check >= 150:
                    reasoning_len_at_last_check = total_reasoning_len
                    if _detect_reasoning_loop("".join(reasoning_parts)):
                        looped = True

                if looped:
                    sys.stdout.write(C.RESET + "\n")
                    print(
                        paint("⚠ 检测到重复思考循环，已中断本次生成", C.ERR)
                    )
                    try:
                        resp.close()
                    except Exception:
                        pass
                    break

            if content:
                clear_spinner()
                if thinking:
                    sys.stdout.write(C.RESET + "\n\n")
                    thinking = False
                if not replied:
                    sys.stdout.write(paint("⏺", C.REPLY_ICON) + " " + C.REPLY)
                    sys.stdout.flush()
                    replied = True
                write_stream(content, C.REPLY, indent="")
                content_parts.append(content)

            if tool_calls:
                clear_spinner()
                if thinking:
                    sys.stdout.write(C.RESET + "\n")
                    thinking = False
                for tc in tool_calls:
                    idx = tc.get("index", 0)
                    slot = tool_acc.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if tc.get("type"):
                        slot["type"] = tc["type"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

    clear_spinner()
    if (thinking or replied) and not looped:
        sys.stdout.write(C.RESET + "\n")

    tools = [tool_acc[i] for i in sorted(tool_acc)]
    # drop empty tool stubs
    tools = [t for t in tools if t["function"]["name"]]
    if looped:
        # 循环时工具调用大概率不完整/无意义，丢弃，交给上层用提示重试
        tools = []
    return "".join(content_parts), tools, "".join(reasoning_parts), looped

def run_agent_turn(messages: list[dict]) -> None:
    """
    Agent 主循环：反复「问模型 → 执行工具 → 把结果喂回模型」，直到模型给出最终回答。

    还包含防循环逻辑：相同编辑重复、同一 build 错误多次、连续瞎 grep 等。
    第一次 Ctrl+C 会取消本轮任务并回到输入提示（不退出程序）。
    """
    global AUTO_APPROVE
    # 每条新用户消息开始时，重置「本轮自动」；全局 /auto 不受影响
    AUTO_APPROVE = False

    recent_sigs: list[str] = []
    build_error_hist: list[str] = []
    searched_this_stuck = False
    reasoning_abort_count = 0
    research_call_count = 0  # 连续 web_search/fetch_url 次数，中间没有真正去改代码
    grep_streak = 0  # 连续 grep_search 次数，用于识别"逐个属性瞎猜"
    # 本轮开始时的消息长度：中断时丢掉未完成的 assistant/tool 片段，保留用户消息
    start_len = len(messages)

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            content, tool_calls, reasoning, looped = chat_once(messages)

            if looped:
                reasoning_abort_count += 1
                if reasoning_abort_count > MAX_REASONING_ABORTS:
                    print(
                        paint(
                            f"✗ 模型连续 {reasoning_abort_count} 次陷入重复思考，已停止本轮。",
                            C.ERR,
                        )
                        + paint("可尝试换个说法、拆小任务，或换更大的模型。", C.DIM, C.STATUS)
                    )
                    print()
                    return
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[系统提示] 你刚才的思考陷入了重复循环，已被中断。"
                            "不要再长篇分析或重复相同句子，直接给出下一步工具调用，"
                            "或者如果已经有足够信息就直接给出简短结论。"
                        ),
                    }
                )
                continue

            if tool_calls:
                assistant_msg: dict = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
                if reasoning:
                    assistant_msg["reasoning_content"] = reasoning
                messages.append(assistant_msg)

                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw_args = tc["function"].get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                        result = f"ERROR: invalid JSON arguments: {raw_args}"
                        show_tool_call(name, args)
                        show_tool_result(result)
                    else:
                        sig = tool_call_signature(name, args)
                        # Block identical mutating edits looping
                        if (
                            name in {"edit_file", "replace_lines", "insert_lines", "delete_lines"}
                            and recent_sigs.count(sig) >= 1
                        ):
                            result = (
                                "ERROR: LOOP — identical edit already tried. "
                                "Do NOT repeat. Call web_search with the exact build error, "
                                "then apply a different fix (or rewrite the broken component)."
                            )
                            show_tool_call(name, args)
                            show_tool_result(result)
                        else:
                            show_tool_call(name, args)
                            if not ask_tool_approval(name, args):
                                result = "ERROR: user denied tool execution"
                                show_tool_result(result)
                            else:
                                result = execute_tool(name, args)
                                show_tool_result(result)
                            recent_sigs.append(sig)
                            if len(recent_sigs) > 24:
                                recent_sigs = recent_sigs[-24:]

                        # Track repeated command failures → force web search hint
                        if name == "run_command" and (
                            "exit=1" in result
                            or result.startswith("FAIL")
                            or result.startswith("ERROR")
                        ):
                            fp = extract_error_fingerprint(result)
                            if fp:
                                build_error_hist.append(fp)
                                same = sum(1 for x in build_error_hist if x == fp)
                                if same >= 2 and not searched_this_stuck:
                                    hint = (
                                        f"\n\nLOOP_HINT: same error seen {same} times:\n  {fp}\n"
                                        "REQUIRED NEXT STEP: web_search(query= that error + 项目实际用的框架/库名) "
                                        "then fetch_url a relevant result. Do not repeat the previous edit."
                                    )
                                    result = result + hint
                                    searched_this_stuck = True
                                if same >= 3:
                                    hint2 = (
                                        "\n\nESCALATE: local patches failed repeatedly. "
                                        "Rewrite the broken file/component with a known-good pattern "
                                        "for the project's actual framework/library, based on the web_search results "
                                        "you already gathered — don't keep micro-tweaking the same lines."
                                    )
                                    if hint2 not in result:
                                        result = result + hint2

                        if name == "web_search":
                            searched_this_stuck = True

                        # 连续搜索/抓取但一直不落地改代码 → 强制收敛，别再换个说法接着搜
                        if name in {"web_search", "fetch_url"}:
                            research_call_count += 1
                            if research_call_count >= 3:
                                result = result + (
                                    "\n\nSTOP_SEARCHING: 你这一轮已经连续调用了 "
                                    f"{research_call_count} 次 web_search/fetch_url，还没有真正去改代码。"
                                    "不要再搜索或换个说法重新搜索，直接根据目前已经拿到的信息"
                                    "用 edit_file/replace_lines "
                                    "对文件做一次具体修改，然后用 run_command 重新跑检查/构建看结果。"
                                )
                        elif name in {"edit_file", "replace_lines", "insert_lines", "delete_lines", "write_file"}:
                            research_call_count = 0

                        # 连续 grep_search（常见于无目的枚举）→ 提醒回到报错本身
                        if name == "grep_search":
                            grep_streak += 1
                            if grep_streak >= 3:
                                result = result + (
                                    "\n\nSTOP_GUESSING: 你连续用 grep_search 了 "
                                    f"{grep_streak} 次，这是在瞎猜。"
                                    "请回到报错信息里的文件名/行号/标识符，用 read_file 读上下文，"
                                    "或 web_search 搜完整报错；不要再无目的地枚举关键词。"
                                )
                        else:
                            grep_streak = 0

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id") or name,
                            "content": result,
                        }
                    )
                print()
                continue

            # final answer
            if content:
                messages.append({"role": "assistant", "content": content})
            elif not content and not tool_calls:
                print(paint("(empty response)", C.DIM, C.STATUS))
            print()
            return

        print(
            paint("✗ 工具轮次达到上限", C.ERR)
            + paint(f"（{MAX_TOOL_ROUNDS}），请再发一条消息让模型继续", C.DIM, C.STATUS)
        )
    except KeyboardInterrupt:
        sys.stdout.write(C.RESET + "\n")
        print(paint("⚠ 已取消当前任务（提示符下再按 Ctrl+C 退出）", C.ERR))
        print()
        del messages[start_len:]
        AUTO_APPROVE = False
        flush_input_buffer()

# ========================================================================
#  第 10 区：主程序入口

# ========================================================================
# 直接运行本文件时从这里开始。被 import 时不会自动执行 main。
#

def read_input(model_id: str) -> str:
    """打印提示符，读取用户输入的一行文字。"""
    sep()
    print(paint(f"  {model_id}", C.STATUS) + paint("  ·  enter to send", C.DIM, C.STATUS))
    sys.stdout.write(paint("❯", C.PROMPT) + " ")
    sys.stdout.flush()
    try:
        line = input()
    except (EOFError, KeyboardInterrupt):
        raise
    return line.strip()

def main() -> int:
    """
    程序入口：等服务就绪 → 显示横幅 → 循环读用户输入 → 交给 run_agent_turn。

    返回 0 表示正常退出，1 表示连不上本地模型服务。
    初学者可顺着下面 1) 2) 3)… 编号阅读启动流程。
    """
    global AUTO_APPROVE, AUTO_APPROVE_ALWAYS
    # 1) Windows 下打开彩色终端
    enable_ansi()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")

    # 2) 等 llama-server 就绪（start.bat 已在后台启动它）
    if not wait_ready():
        print(paint(f"✗  cannot reach {BASE}", C.ERR))
        return 1

    try:
        models = request_json("GET", "/v1/models", timeout=10)
        model_id = ((models.get("data") or [{}])[0]).get("id", "unknown")
    except Exception:
        model_id = "loaded"
    model_id = os.path.basename(model_id)

    # 3) 查出模型名，打印欢迎信息
    print_banner(model_id)
    # 4) 对话历史：第一条永远是 system 提示词
    messages: list[dict] = [{"role": "system", "content": build_system_prompt()}]

    # 5) 主循环：读用户输入 → 处理斜杠命令 → 交给 Agent

    while True:
        try:
            user = read_input(model_id)
        except (EOFError, KeyboardInterrupt):
            print(paint("\n\n  bye.", C.DIM, C.STATUS))
            break

        if not user:
            continue
        if user.lower() in EXIT_CMDS:
            print(paint("\n  bye.", C.DIM, C.STATUS))
            break
        if user.lower() in CLEAR_CMDS:
            messages = [{"role": "system", "content": build_system_prompt()}]
            AUTO_APPROVE = False
            print(paint("\n  ✓ conversation cleared\n", C.TEAL))
            continue
        if user.lower() in AUTO_CMDS:
            AUTO_APPROVE_ALWAYS = True
            print(paint("\n  ✓ 已开启全程自动执行（/manual 关闭）\n", C.TEAL))
            continue
        if user.lower() in MANUAL_CMDS:
            AUTO_APPROVE = False
            AUTO_APPROVE_ALWAYS = False
            print(paint("\n  ✓ 已恢复每次确认\n", C.TEAL))
            continue

        print()
        # Remind model to keep absolute paths verbatim
        abs_paths = extract_abs_paths(user)
        user_payload = user
        if abs_paths:
            joined = " | ".join(abs_paths)
            user_payload = (
                user
                + f"\n\n[系统路径提示] 请原样使用这些绝对路径调用工具，不要改成相对路径：{joined}"
            )
        messages.append({"role": "user", "content": user_payload})

        try:
            # 真正干活：多轮工具调用直到模型给出最终回答
            run_agent_turn(messages)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            print(paint(f"✗  HTTP {e.code}: {detail}", C.ERR))
            messages.pop()
            continue
        except Exception as e:
            print(paint(f"✗  {e}", C.ERR))
            messages.pop()
            continue

    return 0

# Python 惯例：只有「python chat.py」直接运行时，__name__ 才等于 "__main__"。
# raise SystemExit(main())：用 main 的返回值作为进程退出码（给 start.bat 用）。
if __name__ == "__main__":
    raise SystemExit(main())
