"""入口：选模型 → 起 llama-server → 打印接口 / 隧道提示 → 阻塞保活。"""

from __future__ import annotations

import sys
import time

from agent import config, server
from agent.process_guard import install as install_process_guard
from agent.render import console, error, info


def _print_endpoint_hints() -> None:
    """打印本机接口与隧道命令。

    隧道把本机端口映射成公网临时地址，任何客户端（SDK / curl / 其他 harness）
    都能调用；本机地址通常只有本机能用。
    """
    host, port = config.CONNECT_HOST, config.PORT
    local = f"http://{host}:{port}"
    bundled = config.ROOT_DIR / "cloudflared-windows-amd64.exe"

    console.print()
    console.print("[bold]本地模型服务已就绪[/]")
    n_ctx = config.MODEL_N_CTX
    if n_ctx >= 1024:
        ctx_line = f"{n_ctx}（约 {n_ctx / 1024:.0f}k）"
    elif n_ctx:
        ctx_line = str(n_ctx)
    else:
        ctx_line = "未知（可打开 /props 查看 n_ctx）"
    console.print(f"  实际上下文：[cyan]{ctx_line}[/]")
    console.print(f"  OpenAI 兼容接口：[blue]{local}/v1[/]")
    console.print(f"  网页界面：[blue]{local}[/]")
    console.print()
    console.print("  [dim]另开一个终端，把本机接口暴露到公网：[/]")
    console.print(f"    [green]cloudflared tunnel --url {local} --protocol http2[/]")
    console.print(f"    [green]ngrok http {port}[/]")
    if bundled.is_file():
        console.print("  [dim]或用本目录自带程序：[/]")
        console.print(
            f"    [green]{bundled.name} tunnel --url {local} --protocol http2[/]"
        )
    console.print()
    console.print("  把打印出的 [cyan]https://…[/] 后面加上 [cyan]/v1[/]，作为客户端 Base URL。")
    console.print()
    console.print("  [bold]Claude Code[/]（base URL 不带 /v1，它自己拼 /v1/messages）：")
    console.print(f"    [green]ANTHROPIC_BASE_URL={local}[/]")
    console.print("    [green]ANTHROPIC_AUTH_TOKEN=local[/]  [dim]（任意非空值）[/]")
    console.print("    [green]ANTHROPIC_MODEL=claude-sonnet-4-5[/]  [dim]（claude-haiku-4-5 / claude-opus-4-7 同）[/]")
    console.print("    [green]ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7[/]")
    console.print("    [green]ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-5[/]")
    console.print("    [green]ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5[/]")
    console.print("    [green]CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=34[/]")
    if n_ctx < 200 * 1024:
        console.print(
            f"      [dim]（34% × 200k ≈ {200 * 1024 * 34 // 100}，压在本机 ctx={n_ctx} 之下）[/]"
        )
    console.print("    [green]CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192[/]")
    console.print()
    console.print(
        "  [dim]模型名随便填本服务都认，但不能是 Opus 4.8 之后的新模型 id："
        "Claude Code 会往对话中间插 role=system 的消息，这个 GGUF 的模板不允许。[/]"
    )
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

    _print_endpoint_hints()
    info(f"服务运行中（{config.CONNECT_HOST}:{config.PORT}）。按 Ctrl+C 结束。")

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
