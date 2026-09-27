"""托管本地 llama-server：列模型 → 选模型 → 启动/等待就绪 → 读 /props → 停止/切换。

之前这些逻辑在 start.bat 里；搬进 Python 后加载错误能直接看到，
也能在会话中 /model 切换模型而不丢对话。
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from rich.table import Table

from agent import config
from agent.render import console, error, info, warn

_PARAMS_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z0-9])")
# 三值/二值自定义量化（官方 llama.cpp 不认识）；本项目不支持，列模型时跳过
_UNSUPPORTED_QUANT_RE = re.compile(r"(?:^|[-_.])(PTQ1_0|PQ2_0)(?:[-_.]|$)", re.I)
_LOG_TAIL = 60
_HEALTH_TIMEOUT_S = 600  # 大模型 / 慢盘加载可能很久

# 加载日志里值得给用户看的进度行
_PROGRESS_PATTERNS = (
    re.compile(r"offloaded\s+\d+/\d+\s+layers", re.I),
    re.compile(r"n_ctx\s*=\s*\d+", re.I),
    re.compile(r"loading model", re.I),
    re.compile(r"load_tensors", re.I),
    re.compile(r"fit", re.I),
    re.compile(r"CUDA\d*\s+model buffer size", re.I),
    re.compile(r"KV buffer size", re.I),
    re.compile(r"server is listening", re.I),
)
_ERROR_RE = re.compile(r"error|failed|out of memory|cannot|unable", re.I)


# 思考深度英文档位 → 中文显示
_EFFORT_LABELS = {
    "minimal": "最低", "low": "低", "medium": "中",
    "high": "高", "xhigh": "极高", "default": "默认",
}


def _read_gguf_meta(path: Path) -> tuple[str, int]:
    """只读 GGUF 头部 KV：返回 (chat_template, context_length)。context_length=0 表示未知。"""
    import struct
    fixed = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    template = ""
    n_ctx_train = 0
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return "", 0
            f.read(4)  # version
            struct.unpack("<Q", f.read(8))  # tensor count
            n_kv = struct.unpack("<Q", f.read(8))[0]

            def rd_str_bytes() -> bytes:
                ln = struct.unpack("<Q", f.read(8))[0]
                return f.read(ln)

            def read_value(t: int):
                if t == 0:
                    return struct.unpack("<B", f.read(1))[0]
                if t == 1:
                    return struct.unpack("<b", f.read(1))[0]
                if t == 2:
                    return struct.unpack("<H", f.read(2))[0]
                if t == 3:
                    return struct.unpack("<h", f.read(2))[0]
                if t == 4:
                    return struct.unpack("<I", f.read(4))[0]
                if t == 5:
                    return struct.unpack("<i", f.read(4))[0]
                if t == 6:
                    return struct.unpack("<f", f.read(4))[0]
                if t == 7:
                    return struct.unpack("<B", f.read(1))[0]
                if t == 8:
                    return rd_str_bytes().decode("utf-8", "replace")
                if t == 10:
                    return struct.unpack("<Q", f.read(8))[0]
                if t == 11:
                    return struct.unpack("<q", f.read(8))[0]
                if t == 12:
                    return struct.unpack("<d", f.read(8))[0]
                if t == 9:  # array — 这里用不到，直接跳过
                    et = struct.unpack("<I", f.read(4))[0]
                    cnt = struct.unpack("<Q", f.read(8))[0]
                    if et == 8:
                        for _ in range(cnt):
                            ln = struct.unpack("<Q", f.read(8))[0]
                            f.seek(ln, 1)
                    else:
                        f.seek(fixed.get(et, 0) * cnt, 1)
                    return None
                f.seek(fixed.get(t, 0), 1)
                return None

            for _ in range(n_kv):
                key = rd_str_bytes().decode("utf-8", "replace")
                vtype = struct.unpack("<I", f.read(4))[0]
                if key == "tokenizer.chat_template" and vtype == 8:
                    template = rd_str_bytes().decode("utf-8", "replace")
                elif key.endswith(".context_length") and vtype in (0, 1, 2, 3, 4, 5, 10, 11):
                    val = read_value(vtype)
                    try:
                        n = int(val)  # type: ignore[arg-type]
                        if n > n_ctx_train:
                            n_ctx_train = n
                    except (TypeError, ValueError):
                        pass
                else:
                    read_value(vtype)
                    continue
                # context_length / template 已在上面消费；其它分支走 read_value
    except Exception:
        return template, n_ctx_train
    return template, n_ctx_train


def _parse_reasoning_levels(template: str) -> list[str]:
    """从 chat template 里解析支持的思考深度档位（按 low→high 排序）。

    只有模板显式用了 reasoning_effort 才算「可调深度」；Qwen3 那种只有
    enable_thinking 开关的不算。允许的字面量从模板里的白名单 (`not in (...)`)
    提取，避免传模型不认识的值触发模板异常。
    """
    if "reasoning_effort" not in template:
        return []
    order = ["minimal", "low", "medium", "high", "xhigh"]
    found: set[str] = set()
    for m in re.finditer(r"reasoning_effort[^\n]*?in\s*\(([^)]*)\)", template):
        for lit in re.findall(r"['\"]([a-zA-Z]+)['\"]", m.group(1)):
            if lit in order:
                found.add(lit)
    if not found:
        # 用了 reasoning_effort 但没写白名单：给一组通用档位
        found = {"low", "medium", "high"}
    return [x for x in order if x in found]


# path -> (mtime, reasoning_levels, n_ctx_train)
_meta_cache: dict[str, tuple[float, list[str], int]] = {}


def _gguf_meta(path: Path) -> tuple[list[str], int]:
    """带缓存地取思考深度档位与模型标称上下文。"""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return [], 0
    hit = _meta_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    template, n_ctx_train = _read_gguf_meta(path)
    levels = _parse_reasoning_levels(template)
    _meta_cache[key] = (mtime, levels, n_ctx_train)
    return levels, n_ctx_train


def reasoning_levels(path: Path) -> list[str]:
    """带缓存地取某模型支持的思考深度档位（按文件 mtime 失效）。"""
    return _gguf_meta(path)[0]


def n_ctx_train(path: Path) -> int:
    """GGUF 元数据里的标称上下文（如 qwen35.context_length）；未知为 0。"""
    return _gguf_meta(path)[1]


@dataclass
class ModelInfo:
    path: Path
    size_gb: float
    params_b: float | None
    mmproj: Path | None
    n_ctx_train: int = 0

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def reasoning_levels(self) -> list[str]:
        return reasoning_levels(self.path)


class _Proc:
    proc: subprocess.Popen | None = None
    log: list[str] = []
    model: ModelInfo | None = None
    owned = False  # 是否由本进程启动（attach 到已运行服务时为 False）


_state = _Proc()


# ------------------------------------------------------------------------
#  模型列表 / 选择
# ------------------------------------------------------------------------

def parse_params_b(name: str) -> float | None:
    """从文件名里猜参数量：Qwen3.5-4B-Q4_K_M → 4.0；Ornith-1.5-9B → 9.0。"""
    hits = [float(m.group(1)) for m in _PARAMS_RE.finditer(name)]
    hits = [h for h in hits if 0.1 <= h <= 2000]
    return max(hits) if hits else None


def list_models() -> list[ModelInfo]:
    if not config.MODEL_DIR.is_dir():
        return []
    out: list[ModelInfo] = []
    for p in sorted(config.MODEL_DIR.glob("*.gguf"), key=lambda x: x.name.lower()):
        if "mmproj" in p.name.lower():
            continue
        if _UNSUPPORTED_QUANT_RE.search(p.stem):
            continue  # 三值/二值量化，官方 llama-server 无法加载
        stem = p.with_suffix("")
        mm = Path(str(stem) + ".mmproj.gguf")
        out.append(
            ModelInfo(
                path=p,
                size_gb=p.stat().st_size / (1024**3),
                params_b=parse_params_b(p.name),
                mmproj=mm if mm.is_file() else None,
                n_ctx_train=n_ctx_train(p),
            )
        )
    return out


def _orphan_mmproj() -> Path | None:
    for p in sorted(config.MODEL_DIR.glob("mmproj*.gguf")):
        return p
    return None


def _read_last_model() -> str:
    try:
        return config.LAST_MODEL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_last_model(name: str) -> None:
    try:
        config.LAST_MODEL_FILE.write_text(name, encoding="utf-8")
    except OSError:
        pass


def pick_model(allow_attach: bool = False) -> ModelInfo | str | None:
    """交互选择。返回 ModelInfo / "attach" / None（取消）。"""
    models = list_models()
    if not models and not allow_attach:
        error("models\\ 下没有 .gguf 文件")
        console.print("  [dim]把支持 tool calling 的 GGUF 放进 models\\ 后再启动[/]")
        return None

    last = _read_last_model()
    default_idx = 1
    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 1))
    table.add_column("#", justify="right", style="cyan")
    table.add_column("模型")
    table.add_column("大小", justify="right", style="dim")
    table.add_column("参数", justify="right", style="dim")
    table.add_column("上下文", justify="right", style="dim")
    table.add_column("视觉", style="dim")
    table.add_column("思考深度", style="dim")
    for i, m in enumerate(models, 1):
        mark = ""
        if m.name == last:
            default_idx = i
            mark = " [dim](上次)[/]"
        levels = m.reasoning_levels
        depth = "/".join(_EFFORT_LABELS.get(x, x) for x in levels) if levels else "-"
        if m.n_ctx_train >= 1024:
            ctx_s = f"{m.n_ctx_train / 1024:.0f}k"
        elif m.n_ctx_train:
            ctx_s = str(m.n_ctx_train)
        else:
            ctx_s = "-"
        table.add_row(
            str(i),
            m.name + mark,
            f"{m.size_gb:.1f} GB",
            f"{m.params_b:g}B" if m.params_b else "-",
            ctx_s,
            "mmproj" if m.mmproj else "",
            depth,
        )
    extra_idx = len(models)
    attach_idx = 0
    if allow_attach:
        extra_idx += 1
        attach_idx = extra_idx
        table.add_row(
            str(attach_idx),
            f"↪ 使用已在运行的 llama-server ({config.HOST}:{config.PORT})（退出时关闭）",
            "", "", "", "", "",
        )

    console.print()
    console.print("[bold]选择模型[/]")
    console.print(table)
    while True:
        try:
            raw = console.input(f"[dim]输入序号 [1-{extra_idx}]，回车 = {default_idx}: [/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return None
        if not raw:
            choice = default_idx
        elif raw.isdigit():
            choice = int(raw)
        else:
            warn("请输入数字")
            continue
        if not 1 <= choice <= extra_idx:
            warn(f"超出范围，输入 1-{extra_idx}")
            continue
        if choice == attach_idx:
            return "attach"
        m = models[choice - 1]
        _write_last_model(m.name)
        config.REASONING_EFFORT = _pick_reasoning_effort(m)
        return m


def _pick_reasoning_effort(model: ModelInfo) -> str:
    """模型模板支持思考深度时追问一次；返回英文档位，空串 = 用模型默认。"""
    levels = model.reasoning_levels
    if not levels:
        return ""
    lines = []
    for i, lv in enumerate(levels, 1):
        cn = _EFFORT_LABELS.get(lv, lv)
        lines.append(f"    {i} {cn} ({lv})")
    console.print(f"  [dim]该模型支持思考深度：[/]")
    console.print("\n".join(f"  [dim]{ln}[/]" for ln in lines))
    while True:
        try:
            raw = console.input(f"  [dim]选择 [1-{len(levels)}]，回车 = 用模型默认: [/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return ""
        if not raw:
            return ""
        if raw.isdigit() and 1 <= int(raw) <= len(levels):
            return levels[int(raw) - 1]
        warn(f"输入 1-{len(levels)} 或直接回车")


# ------------------------------------------------------------------------
#  HTTP 辅助
# ------------------------------------------------------------------------

def _get_json(path: str, timeout: float = 2.0) -> dict | None:
    try:
        req = urllib.request.Request(f"http://{config.HOST}:{config.PORT}{path}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def is_healthy() -> bool:
    data = _get_json("/health")
    return bool(data) and str(data.get("status", "")).lower() == "ok"


def props() -> dict:
    return _get_json("/props", timeout=3.0) or {}


def apply_props() -> dict:
    """读 /props 并写进 config 的运行时字段（n_ctx / 模型名）。"""
    data = props()
    gen = data.get("default_generation_settings") or {}
    n_ctx = gen.get("n_ctx") or data.get("n_ctx") or 0
    try:
        config.MODEL_N_CTX = int(n_ctx)
    except (TypeError, ValueError):
        config.MODEL_N_CTX = 0
    model_path = str(data.get("model_path") or "")
    if model_path and not config.MODEL_LABEL:
        config.MODEL_LABEL = Path(model_path).name
        config.MODEL_PARAMS_B = parse_params_b(config.MODEL_LABEL)
    return data


# ------------------------------------------------------------------------
#  启动 / 停止
# ------------------------------------------------------------------------

def _exe_in(dir_path: Path) -> Path:
    return dir_path / ("llama-server.exe" if os.name == "nt" else "llama-server")


def resolve_exe() -> Path | None:
    """官方 llama-server：固定用 bin\\。"""
    exe = _exe_in(config.BIN_DIR)
    return exe if exe.is_file() else None


_help_cache: dict[str, str] = {}


def _help_text(exe: Path) -> str:
    """缓存 `llama-server --help`，用来判断这份二进制支持哪些参数。"""
    key = str(exe)
    if key not in _help_cache:
        try:
            r = subprocess.run([key, "--help"], capture_output=True, timeout=30)
            _help_cache[key] = (r.stdout + r.stderr).decode("utf-8", errors="replace")
        except Exception:
            _help_cache[key] = ""  # 探测失败：按「都支持」处理，交给日志报错
    return _help_cache[key]


def _supports(exe: Path, flag: str) -> bool:
    help_text = _help_text(exe)
    return not help_text or flag in help_text


def _kill_stale() -> None:
    """结束残留的 llama-server（比如上次异常退出没关掉）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)


def _pids_listening_on_port(port: int) -> list[int]:
    """返回正在 LISTENING 占用指定端口的 PID 列表。"""
    pids: set[int] = set()
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except Exception:
            return []
        for line in r.stdout.splitlines():
            if "LISTENING" not in line.upper():
                continue
            # 例: TCP  127.0.0.1:8080  0.0.0.0:0  LISTENING  12345
            parts = line.split()
            if len(parts) < 5 or parts[0].upper() not in {"TCP", "UDP"}:
                continue
            local = parts[1]
            try:
                if local.startswith("["):
                    # [::1]:8080
                    idx = local.rfind("]:")
                    if idx < 0 or int(local[idx + 2 :]) != port:
                        continue
                else:
                    _, _, p = local.rpartition(":")
                    if int(p) != port:
                        continue
                pids.add(int(parts[-1]))
            except ValueError:
                continue
    else:
        for cmd in (
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            ["ss", "-lptn", f"sport = :{port}"],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except Exception:
                continue
            if cmd[0] == "lsof" and r.returncode == 0:
                for tok in r.stdout.split():
                    if tok.isdigit():
                        pids.add(int(tok))
                if pids:
                    break
            if cmd[0] == "ss" and r.stdout:
                for m in re.finditer(r"pid=(\d+)", r.stdout):
                    pids.add(int(m.group(1)))
                if pids:
                    break
    # 不要杀自己
    me = os.getpid()
    return sorted(p for p in pids if p != me and p > 0)


def _proc_name(pid: int) -> str:
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            line = (r.stdout or "").strip().splitlines()
            if line and line[0].startswith('"'):
                return line[0].split('","')[0].strip('"')
        except Exception:
            pass
    else:
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            name = (r.stdout or "").strip()
            if name:
                return name
        except Exception:
            pass
    return "?"


def clear_port(port: int | None = None) -> bool:
    """若端口被占用则结束占用进程；释放成功返回 True，失败返回 False。"""
    port = config.PORT if port is None else port
    pids = _pids_listening_on_port(port)
    if not pids:
        return True

    console.print(f"  [dim]端口 {port} 被占用，正在结束占用进程…[/]")
    for pid in pids:
        name = _proc_name(pid)
        console.print(f"  [dim]  PID {pid} ({name})[/]")
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=15,
                )
            else:
                os.kill(pid, 9)
        except Exception as e:
            error(f"无法结束 PID {pid}：{e}")

    # 等端口释放
    for _ in range(20):
        time.sleep(0.25)
        if not _pids_listening_on_port(port):
            info(f"端口 {port} 已释放")
            return True

    left = _pids_listening_on_port(port)
    error(f"端口 {port} 仍被占用：{left}；请手动结束或改 config.json 的 port")
    return False


def _build_cmd(model: ModelInfo, mmproj: Path | None, exe: Path, srv: dict) -> list[str]:
    cmd = [
        str(exe),
        "-m", str(model.path),
        "--host", config.HOST,
        "--port", str(config.PORT),
        "-np", "1",
    ]
    if _supports(exe, "--jinja"):
        cmd += ["--jinja"]
    if _supports(exe, "--tools "):
        cmd += ["--tools", "all"]  # 网页界面自带的内置工具，终端 agent 不依赖它
    if config.REASONING_EFFORT and _supports(exe, "--reasoning-effort"):
        cmd += ["--reasoning-effort", config.REASONING_EFFORT]
    # ctx=None → 不传 -c（llama 默认 0 = 模型标称上限）；显存不够时由 --fit 下调
    # fit_ctx=None → 不传 -fitc（llama 默认下限 4096）
    if srv.get("ngl") is not None:
        cmd += ["-ngl", str(srv["ngl"])]
    if srv.get("ctx") is not None:
        cmd += ["-c", str(srv["ctx"])]
    if srv.get("ngl") is None or srv.get("ctx") is None:
        if _supports(exe, "-fitt"):
            cmd += ["-fitt", str(srv["fit_margin"])]
            if srv.get("fit_ctx") is not None:
                cmd += ["-fitc", str(srv["fit_ctx"])]
        else:
            # 旧基线没有 --fit：尽量全放显存；上下文取手动值或模型标称上限
            if srv.get("ngl") is None:
                cmd += ["-ngl", "99"]
            if srv.get("ctx") is None:
                c = model.n_ctx_train or srv.get("fit_ctx") or 8192
                cmd += ["-c", str(c)]
    if mmproj:
        cmd += ["--mmproj", str(mmproj)]
    cmd += list(srv.get("extra_args") or [])
    return cmd


def _reader(pipe, status_holder: dict) -> None:
    for raw in iter(pipe.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        _state.log.append(line)
        if len(_state.log) > _LOG_TAIL:
            del _state.log[: len(_state.log) - _LOG_TAIL]
        if any(p.search(line) for p in _PROGRESS_PATTERNS):
            status_holder["line"] = line
    try:
        pipe.close()
    except Exception:
        pass


def _resolve_mmproj(model: ModelInfo) -> Path | None:
    if model.mmproj:
        return model.mmproj
    cand = _orphan_mmproj()
    if not cand:
        return None
    console.print(f"  [dim]发现视觉 projector：{cand.name}（只有它是为 {model.name} 生成的才能挂载；"
                  f"重命名为 {model.path.stem}.mmproj.gguf 可跳过此询问）[/]")
    try:
        ans = console.input("  [dim]挂载视觉模块? [y/N]: [/]").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    return cand if ans == "y" else None


def start(model: ModelInfo) -> bool:
    """启动 llama-server 并等待 /health 就绪。失败时把日志尾部打出来。"""
    exe = resolve_exe()
    if exe is None:
        error(f"找不到 {_exe_in(config.BIN_DIR)}")
        return False

    stop()
    if not clear_port(config.PORT):
        return False
    mmproj = _resolve_mmproj(model)
    srv = dict(config.SERVER)
    cmd = _build_cmd(model, mmproj, exe, srv)

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    _state.log = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            # llama-server 的内置文件工具以进程 cwd 解析相对路径。
            # 指向项目根目录，网页里可直接读写当前项目而不是 bin/。
            cwd=str(config.ROOT_DIR),
        )
    except OSError as e:
        error(f"启动 llama-server 失败：{e}")
        return False
    _state.proc = proc
    _state.model = model
    _state.owned = True

    holder: dict = {"line": ""}
    threading.Thread(target=_reader, args=(proc.stdout, holder), daemon=True).start()

    if srv.get("ngl") is None and srv.get("ctx") is None:
        model_ctx = f"{model.n_ctx_train}" if model.n_ctx_train else "?"
        tune = f"自适应显存, 目标 ctx=模型上限 {model_ctx}"
    else:
        tune = f"ngl={srv.get('ngl')} ctx={srv.get('ctx')}"
    console.print(f"[dim]启动 llama-server：{model.name}  ({tune}{', 视觉 on' if mmproj else ''})[/]")
    if config.REASONING_EFFORT and _supports(exe, "--reasoning-effort"):
        cn = _EFFORT_LABELS.get(config.REASONING_EFFORT, config.REASONING_EFFORT)
        console.print(f"  [dim]思考深度：{cn}（--reasoning-effort {config.REASONING_EFFORT}）[/]")

    deadline = time.time() + _HEALTH_TIMEOUT_S
    with console.status("[dim]加载模型…[/]", spinner="dots") as status:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if is_healthy():
                break
            if holder["line"]:
                status.update(f"[dim]加载模型… {holder['line'][:90]}[/]")
            time.sleep(0.4)

    if proc.poll() is not None or not is_healthy():
        error("llama-server 未能就绪" + (f"（退出码 {proc.returncode}）" if proc.poll() is not None else "（超时）"))
        _print_log_tail()
        _print_hints()
        stop()
        return False

    config.MODEL_LABEL = model.name
    config.MODEL_PARAMS_B = model.params_b
    apply_props()
    vision = "视觉 on" if mmproj else "文本"
    info(f"模型就绪：{model.name}  ctx={config.MODEL_N_CTX or '?'}  {vision}")
    console.print(f"  [dim]网页聊天界面（llama-server 自带，Ctrl+点击打开）：[/][blue underline]http://{config.HOST}:{config.PORT}[/]")
    return True


def print_log_tail(n: int = 25) -> None:
    """打印最近若干行 llama-server 日志（失败排查用）。"""
    tail = _state.log[-n:]
    if not tail:
        return
    console.print("[dim]--- llama-server 日志尾部 ---[/]")
    for ln in tail:
        style = "red" if _ERROR_RE.search(ln) else "dim"
        console.print(f"[{style}]{ln}[/]")


# 兼容内部旧名
_print_log_tail = print_log_tail


def _print_hints() -> None:
    text = "\n".join(_state.log).lower()
    if "invalid ggml type" in text:
        warn("这份 llama-server 不认识该量化类型：换标准 GGUF，或升级 bin\\ 到较新的官方构建")
    elif "invalid argument" in text or "unknown argument" in text or "error while handling argument" in text:
        warn("启动参数不被这份 llama-server 支持：检查 config.json 的 server.extra_args")
    elif "out of memory" in text or "cuda" in text and "failed" in text:
        warn("显存不足：把 config.json 里 server.ctx 写小一点（如 8192），或调大 fit_margin；"
             "也可设 fit_ctx 提高 --fit 下限（默认不设 = 用模型上限并按显存下调）")
    elif "failed to load model" in text or "invalid" in text:
        warn("模型文件损坏或 llama-server 版本过旧，无法识别该 GGUF")
    elif "address already in use" in text or "bind" in text:
        warn(f"端口 {config.PORT} 被占用，改 config.json 的 port 或关掉占用程序")
    else:
        warn("可在 bin\\ 目录手动运行 llama-server.exe 查看完整日志")


def stop() -> None:
    """结束本进程拉起的 llama-server；attach 到已有实例时同样关掉，不留后台。"""
    proc = _state.proc
    if proc is not None and proc.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    # attach / 异常残留：按镜像名再清一遍，确保退出后端口与显存都释放
    _kill_stale()
    _state.proc = None
    _state.owned = False
    _state.model = None


atexit.register(stop)


def current_model() -> ModelInfo | None:
    return _state.model


def owned_proc() -> subprocess.Popen | None:
    """本进程拉起的 llama-server；attach 模式为 None。"""
    return _state.proc if _state.owned else None


def model_id() -> str:
    """Cursor 可填的模型 id：优先 /v1/models，否则用 MODEL_LABEL。"""
    data = _get_json("/v1/models", timeout=3.0) or {}
    for item in data.get("data") or []:
        mid = str(item.get("id") or "").strip()
        if mid:
            return mid
    return config.MODEL_LABEL or ""


# ------------------------------------------------------------------------
#  启动流程 / 切换
# ------------------------------------------------------------------------

def ensure_backend() -> bool:
    """程序启动时：选本地 GGUF 并启动 llama-server。返回是否就绪。"""
    already = is_healthy()
    choice = pick_model(allow_attach=already)
    if choice is None:
        return False
    if choice == "attach":
        apply_props()
        info(f"已接入运行中的服务 {config.HOST}:{config.PORT}")
        return True
    return start(choice)


def switch_model() -> bool:
    """重新选一个本地 GGUF 并重启服务。"""
    choice = pick_model(allow_attach=False)
    if choice is None:
        return False
    if _state.model is not None and choice.path == _state.model.path and is_healthy():
        info("已经是当前模型")
        return True
    config.MODEL_LABEL = ""
    config.MODEL_N_CTX = 0
    return start(choice)
