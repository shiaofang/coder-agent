"""主程序入口：等服务就绪 → 横幅 → 读输入 → Agent 循环。

推荐阅读顺序（初学者）：
  main() → run_agent_turn() → chat_once() → execute_tool() → 任意一个 tool_xxx
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

from agent import config
from agent.config import (
    AUTO_CMDS,
    BASE,
    CD_CMD,
    CLEAR_CMDS,
    EXIT_CMDS,
    MANUAL_CMDS,
    PWD_CMDS,
)
from agent.loop import run_agent_turn
from agent.model import wait_ready
from agent.paths import extract_abs_paths, resolve_path, switch_cwd, _looks_like_path_input
from agent.prompts import build_system_prompt
from agent.terminal import C, enable_ansi, paint, print_banner, read_input

def _resolve_target_dir(path_candidate: str) -> Path | None:
    """把用户给的路径（文件或目录、相对或绝对）解析成一个可切换进去的目录；解析不到返回 None。"""
    resolved = resolve_path(path_candidate)
    if resolved.is_dir():
        return resolved
    if resolved.is_file():
        return resolved.parent
    return None

def _startup_dir_from_argv() -> Path | None:
    """支持启动时传一个目录参数，如 `python -m agent.main C:\\project`，
    省得每次进来都要再手动 /cd 或拖文件夹。"""
    if len(sys.argv) < 2:
        return None
    candidate = " ".join(sys.argv[1:]).strip().strip('"').strip("'")
    if not candidate:
        return None
    target = _resolve_target_dir(candidate)
    if target is None:
        print(paint(f"⚠  启动参数不是有效路径，已忽略：{candidate}", C.ERR))
    return target

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

    # 1.5) 云端模式但配置文件缺失/不完整：提前给出友好提示再退出
    if config.CLOUD_CONFIG_ERROR:
        print(paint(f"✗  {config.CLOUD_CONFIG_ERROR}", C.ERR))
        return 1

    # 2) 等模型服务就绪（本地：start.bat 已在后台启动 llama-server；云端：直接跳过）
    if not wait_ready():
        print(paint(f"✗  cannot reach {BASE}", C.ERR))
        return 1

    if config.PROVIDER == "cloud":
        print(paint(f"☁ 云端模型：{config.MODEL_NAME}  ({BASE})", C.DIM, C.STATUS))

    # 2.5) 启动参数里给了目录就先切过去，工作目录在横幅里一起展示
    startup_dir = _startup_dir_from_argv()
    if startup_dir is not None:
        switch_cwd(startup_dir)

    # 3) 欢迎横幅
    print_banner(str(Path.cwd()))
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
            target_dir = _resolve_target_dir(path_candidate)
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
        if cmd in PWD_CMDS:
            print(paint(f"\n  当前工作目录：{Path.cwd()}\n", C.TEAL))
            continue
        if cmd == CD_CMD or cmd.startswith(CD_CMD + " "):
            arg = user_input[len(CD_CMD):].strip().strip('"').strip("'")
            if not arg:
                print(paint(f"\n  当前工作目录：{Path.cwd()}\n", C.TEAL))
                continue
            target_dir = _resolve_target_dir(arg)
            if target_dir is None:
                print(paint(f"\n  ✗ 目录不存在：{resolve_path(arg)}\n", C.ERR))
                continue
            switch_cwd(target_dir)
            messages[0]["content"] = build_system_prompt()
            print(paint(f"\n  ✓ 已切换工作目录：{target_dir}\n", C.TEAL))
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

