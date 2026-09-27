"""终端输出辅助（rich）：模型选择表、启动进度、Cursor 提示用。"""

from __future__ import annotations


def _prepare_windows_console() -> None:
    """开启 VT 转义、stdout UTF-8，避免 Windows 控制台乱码。"""
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

from rich.console import Console

console = Console(highlight=False, soft_wrap=True)


def info(msg: str) -> None:
    console.print(f"  [cyan]✓[/] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [yellow]⚠[/] {msg}")


def error(msg: str) -> None:
    console.print(f"[bold red]✗[/] {msg}")


def dim(msg: str) -> None:
    console.print(f"[dim]{msg}[/]")
