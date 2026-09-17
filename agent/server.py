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


@dataclass
class ModelInfo:
    path: Path
    size_gb: float
    params_b: float | None
    mmproj: Path | None

    @property
    def name(self) -> str:
        return self.path.name


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
        stem = p.with_suffix("")
        mm = Path(str(stem) + ".mmproj.gguf")
        out.append(
            ModelInfo(
                path=p,
                size_gb=p.stat().st_size / (1024**3),
                params_b=parse_params_b(p.name),
                mmproj=mm if mm.is_file() else None,
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


def pick_model(allow_cloud: bool = True, allow_attach: bool = False) -> ModelInfo | str | None:
    """交互选择。返回 ModelInfo / "cloud" / "attach" / None（取消）。"""
    models = list_models()
    show_cloud = allow_cloud and config.cloud_available()
    if not models and not show_cloud and not allow_attach:
        error(f"models\\ 下没有 .gguf 文件，config.json 也没配云端模型")
        console.print("  [dim]本地：把 GGUF 放进 models\\ ；云端：复制 config.example.json 为 config.json 填好 base_url / model[/]")
        return None

    last = _read_last_model()
    default_idx = 1
    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 1))
    table.add_column("#", justify="right", style="cyan")
    table.add_column("模型")
    table.add_column("大小", justify="right", style="dim")
    table.add_column("参数", justify="right", style="dim")
    table.add_column("视觉", style="dim")
    for i, m in enumerate(models, 1):
        mark = ""
        if m.name == last:
            default_idx = i
            mark = " [dim](上次)[/]"
        table.add_row(
            str(i),
            m.name + mark,
            f"{m.size_gb:.1f} GB",
            f"{m.params_b:g}B" if m.params_b else "-",
            "mmproj" if m.mmproj else "",
        )
    extra_idx = len(models)
    cloud_idx = attach_idx = 0
    if show_cloud:
        extra_idx += 1
        cloud_idx = extra_idx
        mark = " [dim](上次)[/]" if last == "cloud" else ""
        # 上次用的是云端，或没有记录但 config.json 写的是 provider=cloud → 默认选云端
        if last == "cloud" or (not last and config.PROVIDER == "cloud"):
            default_idx = cloud_idx
        table.add_row(str(cloud_idx), f"☁ 云端 {config.MODEL_NAME}{mark}", "", "", "")
    if allow_attach:
        extra_idx += 1
        attach_idx = extra_idx
        table.add_row(str(attach_idx), f"↪ 使用已在运行的 llama-server ({config.HOST}:{config.PORT})", "", "", "")

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
        if choice == cloud_idx:
            _write_last_model("cloud")
            return "cloud"
        if choice == attach_idx:
            return "attach"
        m = models[choice - 1]
        _write_last_model(m.name)
        return m


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

def _kill_stale() -> None:
    """结束残留的 llama-server（比如上次异常退出没关掉）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)


def _build_cmd(model: ModelInfo, mmproj: Path | None) -> list[str]:
    exe = config.BIN_DIR / ("llama-server.exe" if os.name == "nt" else "llama-server")
    srv = config.SERVER
    cmd = [
        str(exe),
        "-m", str(model.path),
        "--host", config.HOST,
        "--port", str(config.PORT),
        "-np", "1",
        "--jinja",
    ]
    # 没写死 -ngl/-c 时交给 --fit 按空闲显存自适应
    if srv.get("ngl") is not None:
        cmd += ["-ngl", str(srv["ngl"])]
    if srv.get("ctx") is not None:
        cmd += ["-c", str(srv["ctx"])]
    if srv.get("ngl") is None or srv.get("ctx") is None:
        cmd += ["-fitt", str(srv["fit_margin"]), "-fitc", str(srv["fit_ctx"])]
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
    exe = config.BIN_DIR / ("llama-server.exe" if os.name == "nt" else "llama-server")
    if not exe.is_file():
        error(f"找不到 {exe}")
        return False

    stop()
    _kill_stale()
    mmproj = _resolve_mmproj(model)
    cmd = _build_cmd(model, mmproj)

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
            cwd=str(config.BIN_DIR),
        )
    except OSError as e:
        error(f"启动 llama-server 失败：{e}")
        return False
    _state.proc = proc
    _state.model = model
    _state.owned = True

    holder: dict = {"line": ""}
    threading.Thread(target=_reader, args=(proc.stdout, holder), daemon=True).start()

    tune = "自适应显存" if config.SERVER.get("ngl") is None and config.SERVER.get("ctx") is None else \
        f"ngl={config.SERVER.get('ngl')} ctx={config.SERVER.get('ctx')}"
    console.print(f"[dim]启动 llama-server：{model.name}  ({tune}{', 视觉 on' if mmproj else ''})[/]")

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


def _print_log_tail(n: int = 25) -> None:
    tail = _state.log[-n:]
    if not tail:
        return
    console.print("[dim]--- llama-server 日志尾部 ---[/]")
    for ln in tail:
        style = "red" if _ERROR_RE.search(ln) else "dim"
        console.print(f"[{style}]{ln}[/]")


def _print_hints() -> None:
    text = "\n".join(_state.log).lower()
    if "out of memory" in text or "cuda" in text and "failed" in text:
        warn("显存不足：把 config.json 里 server.fit_ctx 调小（如 8192），或调大 fit_margin")
    elif "failed to load model" in text or "invalid" in text:
        warn("模型文件损坏或 llama-server 版本过旧，无法识别该 GGUF")
    elif "address already in use" in text or "bind" in text:
        warn(f"端口 {config.PORT} 被占用，改 config.json 的 port 或关掉占用程序")
    else:
        warn("可在 bin\\ 目录手动运行 llama-server.exe 查看完整日志")


def stop() -> None:
    proc = _state.proc
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _state.proc = None
    _state.owned = False


atexit.register(stop)


def current_model() -> ModelInfo | None:
    return _state.model


# ------------------------------------------------------------------------
#  启动流程 / 切换
# ------------------------------------------------------------------------

def ensure_backend() -> bool:
    """程序启动时：决定用云端还是本地；本地则选模型并启动。返回是否就绪。"""
    if config.PROVIDER == "cloud" and config.PROVIDER_FROM_ENV:
        config.MODEL_LABEL = config.MODEL_NAME
        return True

    already = is_healthy()
    if config.PROVIDER_FROM_ENV and config.PROVIDER == "local" and already:
        # start.bat / 手动起好的服务：直接用
        apply_props()
        return True

    choice = pick_model(allow_cloud=not config.PROVIDER_FROM_ENV, allow_attach=already)
    if choice is None:
        return False
    if choice == "cloud":
        if not config.cloud_available():
            error("config.json 缺 base_url / model，无法使用云端")
            return False
        config.use_cloud()
        config.MODEL_LABEL = config.MODEL_NAME
        return True
    if choice == "attach":
        config.use_local()
        apply_props()
        return True
    config.use_local()
    return start(choice)


def switch_model() -> bool:
    """/model：重新选一个本地 GGUF 并重启服务。失败时保留旧服务（若仍在）。"""
    choice = pick_model(allow_cloud=config.cloud_available(), allow_attach=False)
    if choice is None:
        return False
    if choice == "cloud":
        stop()
        config.use_cloud()
        config.MODEL_LABEL = config.MODEL_NAME
        config.MODEL_PARAMS_B = None
        config.MODEL_N_CTX = 0
        info(f"已切换到云端模型 {config.MODEL_NAME}")
        return True
    if _state.model is not None and choice.path == _state.model.path and is_healthy():
        info("已经是当前模型")
        return True
    config.use_local()
    config.MODEL_LABEL = ""
    config.MODEL_N_CTX = 0
    return start(choice)
