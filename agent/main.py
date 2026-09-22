"""主程序入口：选模型/起服务 → 横幅 → 读输入 → Agent 循环。

推荐阅读顺序（初学者）：
  main() → run_agent_turn() → chat_once() → execute_tool() → 任意一个 tool_xxx
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

from agent import config, context, server, session
from agent.config import (
    AUTO_CMDS,
    CD_CMD,
    CLEAR_CMDS,
    COMPACT_CMDS,
    CTX_CMDS,
    EXIT_CMDS,
    HELP_CMDS,
    MANUAL_CMDS,
    MODEL_CMDS,
    PWD_CMDS,
    RESUME_CMD,
    SESSIONS_CMDS,
    SLASH_MENU,
    THINK_CMD,
    VERBOSE_CMDS,
)
from agent.loop import run_agent_turn
from agent.paths import _looks_like_path_input, extract_abs_paths, resolve_path, switch_cwd
from agent.prompts import build_system_prompt
from agent.render import console, ctx_bar, error, info, show_banner, warn
from agent.terminal import enable_ansi, read_input, set_toolbar_provider
from agent.tools import clear_todos, get_todos, set_todos


def _resolve_target_dir(path_candidate: str) -> Path | None:
    """把用户给的路径（文件或目录、相对或绝对）解析成一个可切换进去的目录；解析不到返回 None。"""
    resolved = resolve_path(path_candidate)
    if resolved.is_dir():
        return resolved
    if resolved.is_file():
        return resolved.parent
    return None


def _startup_dir_from_argv() -> Path | None:
    """支持启动时传一个目录参数，如 `python chat.py C:\\project`。"""
    if len(sys.argv) < 2:
        return None
    candidate = " ".join(sys.argv[1:]).strip().strip('"').strip("'")
    if not candidate:
        return None
    target = _resolve_target_dir(candidate)
    if target is None:
        warn(f"启动参数不是有效路径，已忽略：{candidate}")
    return target


def _print_help() -> None:
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column(style="dim")
    for cmd, hint in SLASH_MENU:
        table.add_row(cmd, hint)
    console.print(table)
    console.print("  [dim]多行输入：以 ``` 开头进入多行，再输入 ``` 结束；或 Alt+Enter 换行。"
                  "直接输入/拖入一个路径回车 = 切换工作目录。[/]")
    console.print()


def main() -> int:
    """程序入口。返回 0 表示正常退出，1 表示模型服务不可用。"""
    enable_ansi()
    for stream in (sys.stdout, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if config.CONFIG_ERROR:
        error(config.CONFIG_ERROR)
        return 1

    # 1) 选模型 + 启动 llama-server（或云端）
    if not server.ensure_backend():
        return 1

    # 2) 启动参数里给了目录就先切过去
    startup_dir = _startup_dir_from_argv()
    if startup_dir is not None:
        switch_cwd(startup_dir)

    show_banner(str(Path.cwd()))

    # 3) 对话历史：第一条永远是 system 提示词
    messages: list[dict] = [{"role": "system", "content": build_system_prompt()}]
    sess = session.Session()

    def toolbar() -> str:
        model = (config.MODEL_LABEL or config.MODEL_NAME or "?")
        if len(model) > 34:
            model = model[:31] + "…"
        mode = "auto" if config.AUTO_APPROVE_ALWAYS else "manual"
        think = "think" if config.THINKING else "no-think"
        cwd = str(Path.cwd())
        if len(cwd) > 40:
            cwd = "…" + cwd[-39:]
        return f" {model} │ {context.status_text(messages)} │ {mode} · {think} │ {cwd}"

    set_toolbar_provider(toolbar)

    def refresh_system() -> None:
        messages[0]["content"] = build_system_prompt()

    def switch_and_report(target_dir: Path) -> None:
        switch_cwd(target_dir)
        refresh_system()
        console.print()
        info(f"已切换工作目录：{target_dir}")
        console.print()

    def new_session() -> None:
        nonlocal messages, sess
        messages = [{"role": "system", "content": build_system_prompt()}]
        config.AUTO_APPROVE = False
        clear_todos()
        context.reset_anchor()
        sess = session.Session()

    # 4) 主循环
    while True:
        try:
            user_input = read_input()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]bye.[/]")
            break

        if not user_input:
            continue

        # 直接输入一个路径（拖拽文件夹/文件进终端也算）并回车：切换当前工作目录
        path_candidate = user_input.strip().strip('"').strip("'")
        if _looks_like_path_input(path_candidate):
            target_dir = _resolve_target_dir(path_candidate)
            if target_dir is not None:
                switch_and_report(target_dir)
                continue

        cmd = user_input.lower()
        head, _, arg = user_input.partition(" ")
        head = head.lower()
        arg = arg.strip().strip('"').strip("'")

        if cmd in EXIT_CMDS:
            console.print("\n  [dim]bye.[/]")
            break
        if cmd in HELP_CMDS:
            console.print()
            _print_help()
            continue
        if cmd in CLEAR_CMDS:
            new_session()
            console.print()
            info("已开始新会话")
            console.print()
            continue
        if cmd in AUTO_CMDS:
            config.AUTO_APPROVE_ALWAYS = True
            console.print()
            info("已开启全程自动执行（/manual 关闭）")
            console.print()
            continue
        if cmd in MANUAL_CMDS:
            config.AUTO_APPROVE = False
            config.AUTO_APPROVE_ALWAYS = False
            console.print()
            info("已恢复每次确认")
            console.print()
            continue
        if cmd in PWD_CMDS:
            console.print()
            info(f"当前工作目录：{Path.cwd()}")
            console.print()
            continue
        if head == CD_CMD:
            if not arg:
                console.print()
                info(f"当前工作目录：{Path.cwd()}")
                console.print()
                continue
            target_dir = _resolve_target_dir(arg)
            if target_dir is None:
                console.print()
                error(f"目录不存在：{resolve_path(arg)}")
                console.print()
                continue
            switch_and_report(target_dir)
            continue
        if cmd in MODEL_CMDS:
            if server.switch_model():
                refresh_system()
                context.reset_anchor()
                show_banner(str(Path.cwd()))
                context.check(messages)
            continue
        if cmd in COMPACT_CMDS:
            console.print()
            context.compact_now(messages)
            console.print()
            continue
        if cmd in CTX_CMDS:
            used, total = context.usage(messages)
            console.print()
            console.print(f"  上下文  {ctx_bar(used, total)}")
            n_tools = sum(1 for m in messages if m.get("role") == "tool")
            console.print(f"  [dim]{len(messages)} 条消息 · {n_tools} 条工具结果 · 服务端上次实际 prompt {context.last_prompt_tokens() or '?'} tokens[/]")
            console.print()
            continue
        if cmd in VERBOSE_CMDS:
            config.VERBOSE = not config.VERBOSE
            console.print()
            info("详细模式：" + ("开（工具结果 / 思考 / diff 全量显示）" if config.VERBOSE else "关"))
            console.print()
            continue
        if head == THINK_CMD:
            console.print()
            if arg.lower() in {"on", "1", "true"}:
                config.THINKING = True
            elif arg.lower() in {"off", "0", "false"}:
                config.THINKING = False
            elif not arg:
                config.THINKING = not config.THINKING
            else:
                warn("用法：/think on|off")
                console.print()
                continue
            info("模型思考：" + ("开" if config.THINKING else "关（简单任务更快；复杂任务建议开）"))
            console.print()
            continue
        if cmd in SESSIONS_CMDS:
            console.print()
            session.print_list(session.list_recent())
            console.print()
            continue
        if head == RESUME_CMD:
            console.print()
            items = session.list_recent()
            if not items:
                info("还没有保存的会话")
                console.print()
                continue
            idx = 1
            if arg:
                if not arg.isdigit() or not 1 <= int(arg) <= len(items):
                    session.print_list(items)
                    console.print()
                    continue
                idx = int(arg)
            elif len(items) > 1:
                session.print_list(items)
                try:
                    raw = console.input(f"  [dim]恢复第几条 [1-{len(items)}]，回车 = 1: [/]").strip()
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    continue
                if raw:
                    if not raw.isdigit() or not 1 <= int(raw) <= len(items):
                        warn("序号无效")
                        console.print()
                        continue
                    idx = int(raw)
            loaded = session.load(items[idx - 1])
            if loaded is None:
                error("会话文件损坏，无法恢复")
                console.print()
                continue
            old_msgs, todos, old_cwd = loaded
            if old_cwd and Path(old_cwd).is_dir():
                switch_cwd(Path(old_cwd))
            messages = [{"role": "system", "content": build_system_prompt()}] + [
                m for m in old_msgs if m.get("role") != "system"
            ]
            set_todos(todos)
            context.reset_anchor()
            sess = session.Session()
            n_turns = sum(1 for m in messages if m.get("role") == "user")
            info(f"已恢复会话（{n_turns} 轮，{items[idx - 1].preview}）  目录：{Path.cwd()}")
            context.check(messages)
            console.print()
            continue
        if user_input.startswith("/"):
            console.print()
            warn(f"未知命令：{head}  （/help 查看全部）")
            console.print()
            continue

        console.print()
        # 提醒模型：用户原文里的绝对路径要原样使用
        abs_paths = extract_abs_paths(user_input)
        user_message_content = user_input
        if abs_paths:
            abs_paths_joined = " | ".join(abs_paths)
            user_message_content = (
                user_input
                + f"\n\n[系统路径提示] 请原样使用这些绝对路径调用工具，不要改成相对路径：{abs_paths_joined}"
            )
        # 记住本轮起点：中途异常时把这条用户消息连同未完成的
        # assistant/tool 片段一起丢弃，避免留下残缺的 tool_calls 历史
        turn_start = len(messages)
        messages.append({"role": "user", "content": user_message_content})

        try:
            run_agent_turn(messages)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            error(f"HTTP {e.code}: {detail[:600]}")
            if e.code in (401, 403):
                if config.API_STYLE == "deepseek":
                    warn("deepseek 不认这个密钥：把请求头 Authorization 里 Bearer 后面的内容"
                         "填进 config.json 的 deepseek.api_key")
                else:
                    warn("云端接口不认这个 api_key：检查 config.json 的 api_key 是否有效/完整，"
                         "Ollama 的 key 在 ollama.com/settings/keys 重新生成")
            elif e.code in (400, 413) and ("context" in detail.lower() or "token" in detail.lower()):
                warn("很可能是上下文超限：试试 /compact，或 /new 开新会话")
            del messages[turn_start:]
            continue
        except urllib.error.URLError as e:
            error(f"连不上模型服务：{e.reason}")
            if config.API_STYLE == "deepseek":
                reason = str(e.reason)
                if "getaddrinfo" in reason or "11001" in reason:
                    warn(f"域名解析失败：{config.DEEPSEEK_BASE} 没有 DNS 记录，请求没有发出去")
                else:
                    warn(f"连不上 deepseek（{config.DEEPSEEK_BASE}）。接口还没好时可以用 /model 换回本地模型")
            elif config.PROVIDER == "local":
                warn("llama-server 可能已退出（显存不足 / 崩溃），用 /model 重新启动")
            del messages[turn_start:]
            continue
        except Exception as e:
            error(f"{type(e).__name__}: {e}")
            del messages[turn_start:]
            continue
        finally:
            sess.save(messages, get_todos())

    server.stop()
    return 0
