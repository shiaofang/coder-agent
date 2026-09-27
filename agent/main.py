"""入口：选模型 → 起 llama-server → 提示 Cursor / 隧道 → 阻塞保活。"""

from __future__ import annotations

import os
import sys
import time

from agent import config, server
from agent.process_guard import install as install_process_guard
from agent.render import console, error, info


def _cursor_api_key() -> str:
    return os.environ.get("CURSOR_PROXY_TOKEN", "").strip() or "cursor-local"


def _print_cursor_hints() -> None:
    """打印 Cursor 配置与 cloudflared 隧道命令（本机地址 Cursor 通常访问不到）。"""
    host, port = config.HOST, config.PORT
    model = config.MODEL_LABEL or "（见 /v1/models）"
    # 优先用服务端 /v1/models 的 id
    mid = server.model_id()
    if mid:
        model = mid
    token = _cursor_api_key()
    local = f"http://{host}:{port}"
    bundled = config.ROOT_DIR / "cloudflared-windows-amd64.exe"

    console.print()
    console.print("[bold]Cursor 本地模型已就绪[/]")
    console.print(f"  本机 OpenAI 兼容接口：[blue]{local}/v1[/]")
    console.print(f"  OpenAI API Key：[cyan]{token}[/]")
    console.print(f"  模型名可填：[cyan]{model}[/]")
    console.print()
    console.print("  [dim]Cursor 通常不能直接访问 127.0.0.1。另开一个终端执行隧道：[/]")
    console.print(f"    [green]cloudflared tunnel --url {local} --protocol http2[/]")
    if bundled.is_file():
        console.print("  [dim]或用本目录自带程序：[/]")
        console.print(
            f"    [green]{bundled.name} tunnel --url {local} --protocol http2[/]"
        )
    console.print()
    console.print("  把打印出的 [cyan]https://….trycloudflare.com[/] 加上 [cyan]/v1[/]，填到：")
    console.print("    Cursor Settings → Models → Override OpenAI Base URL")
    console.print(f"  OpenAI API Key 填 [cyan]{token}[/]，添加模型名 [cyan]{model}[/]。")
    console.print()
    console.print("  [dim]本窗口需保持打开。Ctrl+C 或关闭终端会结束 llama-server。[/]")
    console.print()


def main() -> int:
    """返回 0 正常退出，1 表示模型服务不可用。"""
    install_process_guard()
    for stream in (sys.stdout, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if config.CONFIG_ERROR:
        error(config.CONFIG_ERROR)
        return 1

    if not server.ensure_backend():
        return 1

    _print_cursor_hints()
    info(f"服务运行中（{config.HOST}:{config.PORT}）。按 Ctrl+C 结束。")

    try:
        while True:
            time.sleep(1.5)
            proc = server.owned_proc()
            if proc is not None and proc.poll() is not None:
                error("llama-server 进程已退出")
                server.print_log_tail()
                return 1
            if proc is None and not server.is_healthy():
                # attach 模式：外部服务挂了
                error("模型服务已不可用")
                return 1
    except KeyboardInterrupt:
        console.print("\n  [dim]bye.[/]")
    finally:
        server.stop()

    return 0
