"""主程序入口：等服务就绪 → 横幅 → 读输入 → Agent 循环。

推荐阅读顺序（初学者）：
  main() → run_agent_turn() → chat_once() → execute_tool() → 任意一个 tool_xxx
"""

from __future__ import annotations

import sys
import urllib.error

from agent import config
from agent.config import (
    AUTO_CMDS,
    BASE,
    CLEAR_CMDS,
    EXIT_CMDS,
    MANUAL_CMDS,
)
from agent.loop import run_agent_turn
from agent.model import wait_ready
from agent.paths import extract_abs_paths, resolve_path, switch_cwd, _looks_like_path_input
from agent.prompts import build_system_prompt
from agent.terminal import C, enable_ansi, paint, print_banner, read_input

def main() -> int:
    """
    程序入口：等服务就绪 → 显示横幅 → 循环读用户输入 → 交给 run_agent_turn。

    返回 0 表示正常退出，1 表示连不上本地模型服务。
    初学者可顺着下面 1) 2) 3)… 编号阅读启动流程。
    """
    # 1) Windows 下打开彩色终端
    enable_ansi()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")

    # 2) 等 llama-server 就绪（start.bat 已在后台启动它）
    if not wait_ready():
        print(paint(f"✗  cannot reach {BASE}", C.ERR))
        return 1

    # 3) 欢迎横幅
    print_banner()
    # 4) 对话历史：第一条永远是 system 提示词
    messages: list[dict] = [{"role": "system", "content": build_system_prompt()}]

    # 5) 主循环：读用户输入 → 处理斜杠命令 → 交给 Agent
    while True:
        try:
            user_input = read_input()
        except (EOFError, KeyboardInterrupt):
            print(paint("\n\n  bye.", C.DIM, C.STATUS))
            break

        if not user_input:
            continue

        # 直接输入一个路径（拖拽文件夹/文件进终端也算）并回车：切换当前工作目录，
        # 不当作聊天消息发给模型。
        path_candidate = user_input.strip().strip('"').strip("'")
        if _looks_like_path_input(path_candidate):
            resolved_path = resolve_path(path_candidate)
            target_dir = (
                resolved_path
                if resolved_path.is_dir()
                else (resolved_path.parent if resolved_path.is_file() else None)
            )
            if target_dir is not None:
                switch_cwd(target_dir)
                messages[0]["content"] = build_system_prompt()
                print(paint(f"\n  ✓ 已切换工作目录：{target_dir}\n", C.TEAL))
                continue

        cmd = user_input.lower()
        if cmd in EXIT_CMDS:
            print(paint("\n  bye.", C.DIM, C.STATUS))
            break
        if cmd in CLEAR_CMDS:
            messages = [{"role": "system", "content": build_system_prompt()}]
            config.AUTO_APPROVE = False
            print(paint("\n  ✓ conversation cleared\n", C.TEAL))
            continue
        if cmd in AUTO_CMDS:
            config.AUTO_APPROVE_ALWAYS = True
            print(paint("\n  ✓ 已开启全程自动执行（/manual 关闭）\n", C.TEAL))
            continue
        if cmd in MANUAL_CMDS:
            config.AUTO_APPROVE = False
            config.AUTO_APPROVE_ALWAYS = False
            print(paint("\n  ✓ 已恢复每次确认\n", C.TEAL))
            continue

        print()
        # 提醒模型：用户原文里的绝对路径要原样使用
        abs_paths = extract_abs_paths(user_input)
        user_message_content = user_input
        if abs_paths:
            abs_paths_joined = " | ".join(abs_paths)
            user_message_content = (
                user_input
                + f"\n\n[系统路径提示] 请原样使用这些绝对路径调用工具，不要改成相对路径：{abs_paths_joined}"
            )
        messages.append({"role": "user", "content": user_message_content})

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

