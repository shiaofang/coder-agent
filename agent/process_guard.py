"""进程退出兜底：点控制台窗口 X / 注销 / 关机时也关掉 llama-server。

仅靠 atexit 不够——Windows 关窗发的是 CTRL_CLOSE_EVENT，常来不及跑到
正常 finally。这里做两件事：

1. SetConsoleCtrlHandler：关窗瞬间主动 server.stop()
2. Job Object（KILL_ON_JOB_CLOSE）：本进程被强杀时，子进程一并带走
"""

from __future__ import annotations

import os
import signal
from typing import Any

# 必须挂在模块上，防止 ctypes 回调被 GC；Job 句柄也不能提前 Close
_ctrl_handler: Any = None
_job_handle: Any = None
_installed = False


def _cleanup() -> None:
    try:
        from agent import server

        server.stop()
    except Exception:
        pass


def install() -> None:
    """在 main() 开头调一次即可。"""
    global _installed
    if _installed:
        return
    _installed = True
    if os.name == "nt":
        _install_win_job()
        _install_win_ctrl_handler()
    else:
        _install_posix_signals()


def _install_win_job() -> None:
    """把当前进程放进带 KILL_ON_JOB_CLOSE 的 Job；子进程默认随 Job 死。"""
    global _job_handle
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(handle)
        return

    # 已在别的 Job 里时会失败（如某些 IDE 终端）；关窗钩子仍可兜底
    if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
        kernel32.CloseHandle(handle)
        return

    _job_handle = handle


def _install_win_ctrl_handler() -> None:
    """点控制台右上角 X / 注销 / 关机时立刻清理。"""
    global _ctrl_handler
    import ctypes
    from ctypes import wintypes

    HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6

    @HandlerRoutine
    def handler(ctrl_type: int) -> bool:
        if ctrl_type in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            _cleanup()
            return True
        return False

    if ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
        _ctrl_handler = handler


def _install_posix_signals() -> None:
    """终端被关掉时通常收到 SIGHUP / SIGTERM。"""

    def _handler(signum: int, frame: Any) -> None:
        _cleanup()
        raise SystemExit(128 + signum)

    for sig in (getattr(signal, "SIGHUP", None), signal.SIGTERM):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass
