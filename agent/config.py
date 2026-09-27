"""配置：本地 GGUF / llama-server 参数、路径、运行时模型状态。

运行时配置统一放在项目根目录的 config.json（已 gitignore）。
模板见 config.example.json。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT_DIR / "bin"
MODEL_DIR = ROOT_DIR / "models"
LAST_MODEL_FILE = ROOT_DIR / ".last_model"

HOST = "127.0.0.1"
PORT = 8080
BASE = f"http://{HOST}:{PORT}"

CONFIG_ERROR: str | None = None

# llama-server 启动参数（config.json -> "server"）
SERVER: dict = {
    "fit_margin": 384,
    "fit_ctx": None,
    "ngl": None,
    "ctx": None,
    "extra_args": [],
}

CONFIG_PATH = ROOT_DIR / "config.json"


def _load_config() -> None:
    """读取 config.json；NGL / CTX 可用环境变量覆盖。"""
    global HOST, PORT, BASE, CONFIG_ERROR

    data: dict = {}
    path = CONFIG_PATH
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            CONFIG_ERROR = f"config.json 解析失败：{type(e).__name__}: {e}"
            return
        if not isinstance(raw, dict):
            CONFIG_ERROR = "config.json 必须是 JSON 对象"
            return
        data = raw

    if data.get("host") not in (None, ""):
        HOST = str(data["host"]).strip()
    if data.get("port") not in (None, ""):
        try:
            PORT = int(data["port"])
        except (TypeError, ValueError):
            CONFIG_ERROR = "config.json 的 port 必须是整数"
            return

    srv = data.get("server")
    if isinstance(srv, dict):
        for key in ("ngl", "ctx", "fit_ctx"):
            if key not in srv:
                continue
            if srv[key] in (None, ""):
                SERVER[key] = None
                continue
            try:
                SERVER[key] = int(srv[key])
            except (TypeError, ValueError):
                CONFIG_ERROR = f"config.json 的 server.{key} 必须是整数或 null"
                return
        if "fit_margin" in srv and srv["fit_margin"] not in (None, ""):
            try:
                SERVER["fit_margin"] = int(srv["fit_margin"])
            except (TypeError, ValueError):
                CONFIG_ERROR = "config.json 的 server.fit_margin 必须是整数"
                return
        extra = srv.get("extra_args")
        if isinstance(extra, list):
            SERVER["extra_args"] = [str(x) for x in extra]
    for env_key, cfg_key in (("CODER_AGENT_NGL", "ngl"), ("CODER_AGENT_CTX", "ctx")):
        val = os.environ.get(env_key, "").strip()
        if val:
            try:
                SERVER[cfg_key] = int(val)
            except ValueError:
                pass

    BASE = f"http://{HOST}:{PORT}"


_load_config()

# 当前模型信息（server.py 启动后填写）
MODEL_LABEL = ""
MODEL_PARAMS_B: float | None = None
MODEL_N_CTX = 0
REASONING_EFFORT = ""


def n_ctx() -> int:
    """当前上下文长度（未知时回退 8192）。"""
    if MODEL_N_CTX > 0:
        return MODEL_N_CTX
    if SERVER.get("ctx"):
        return int(SERVER["ctx"])
    if SERVER.get("fit_ctx"):
        return int(SERVER["fit_ctx"])
    return 8192
