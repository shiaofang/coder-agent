"""配置常量：本地/云端模型、搜索 Key、斜杠命令、安全上限、写操作确认开关。

运行时配置统一放在项目根目录的 config.json（已 gitignore）。
模板见 config.example.json。会话开关 AUTO_APPROVE / AUTO_APPROVE_ALWAYS
放在本模块，其它文件用 `import agent.config as config` 再读写。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# ========================================================================
#  默认值（可被 config.json / 环境变量覆盖）
# ========================================================================
HOST = "127.0.0.1"
PORT = 8080
BASE = f"http://{HOST}:{PORT}"

# PROVIDER    — "local"（本机 llama-server）或 "cloud"（OpenAI 兼容接口）
# MODEL_NAME  — 云端请求体里的 model；本地可留空
# API_KEY     — 云端鉴权；本地可留空
# CONFIG_ERROR — 配置缺失/不完整时的错误说明，main.py 启动时检查并退出
PROVIDER = "local"
MODEL_NAME = ""
API_KEY = ""
TAVILY_API_KEY = ""
CONFIG_ERROR: str | None = None

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
# 旧文件名：若只有 cloud_config.json，加载时提示迁移
_LEGACY_CONFIG_PATH = Path(__file__).resolve().parent.parent / "cloud_config.json"


def _load_config() -> None:
    """读取 config.json，并用环境变量覆盖（CODER_AGENT_PROVIDER / 搜索 Key）。

    优先级：
      provider — 环境变量 CODER_AGENT_PROVIDER > config.json provider > local
      搜索 Key — 环境变量 TAVILY_API_KEY > config.json tavily_api_key
    """
    global HOST, PORT, BASE, PROVIDER, MODEL_NAME, API_KEY
    global TAVILY_API_KEY, CONFIG_ERROR

    data: dict = {}
    path = CONFIG_PATH
    if not path.exists() and _LEGACY_CONFIG_PATH.exists():
        CONFIG_ERROR = (
            f"检测到旧配置文件：{_LEGACY_CONFIG_PATH.name}\n"
            f"  请重命名为 {CONFIG_PATH.name}（或复制 config.example.json）后再启动"
        )
        return

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

    # —— 本地服务地址 ——
    if data.get("host") not in (None, ""):
        HOST = str(data["host"]).strip()
    if data.get("port") not in (None, ""):
        try:
            PORT = int(data["port"])
        except (TypeError, ValueError):
            CONFIG_ERROR = "config.json 的 port 必须是整数"
            return

    # —— 云端模型字段 ——
    base_url = str(data.get("base_url", "")).strip().rstrip("/")
    MODEL_NAME = str(data.get("model", "")).strip()
    API_KEY = str(data.get("api_key", "")).strip()

    # —— 搜索 Key（环境变量优先）——
    TAVILY_API_KEY = (
        os.environ.get("TAVILY_API_KEY", "").strip()
        or str(data.get("tavily_api_key", "")).strip()
    )

    # —— provider：环境变量（start.bat 菜单）> config.json > local ——
    env_provider = os.environ.get("CODER_AGENT_PROVIDER", "").strip().lower()
    file_provider = str(data.get("provider", "")).strip().lower()
    if env_provider in {"local", "cloud"}:
        PROVIDER = env_provider
    elif file_provider in {"local", "cloud"}:
        PROVIDER = file_provider
    elif file_provider:
        CONFIG_ERROR = 'config.json 的 provider 只能是 "local" 或 "cloud"'
        return
    else:
        PROVIDER = "local"

    if PROVIDER == "cloud":
        if not path.exists():
            CONFIG_ERROR = (
                f"未找到配置文件：{CONFIG_PATH}\n"
                "  请复制 config.example.json 为 config.json 并填好 base_url / model / api_key"
            )
            return
        if not base_url or not MODEL_NAME:
            CONFIG_ERROR = "config.json 在 cloud 模式下需要同时填写 base_url 与 model"
            return
        BASE = base_url
    else:
        BASE = f"http://{HOST}:{PORT}"


_load_config()

# 用户可输入的斜杠命令（不区分大小写，在 main 里处理）
EXIT_CMDS = {"/exit", "/quit", "/q", "exit", "quit"}
CLEAR_CMDS = {"/clear_cache", "/reset", "/new"}
AUTO_CMDS = {"/auto"}
MANUAL_CMDS = {"/manual"}
PWD_CMDS = {"/pwd", "/dir"}
CD_CMD = "/cd"  # 后面带参数（路径），不能放进固定集合，用前缀匹配

# 输入以 / 开头时弹出的命令菜单（展示用；实际匹配仍看上面的集合）
SLASH_MENU: list[tuple[str, str]] = [
    ("/cd", "切换工作目录，如 /cd .. 或 /cd D:\\project"),
    ("/pwd", "查看当前工作目录"),
    ("/clear_cache", "清空对话"),
    ("/auto", "全程自动执行"),
    ("/manual", "每次确认"),
    ("/exit", "退出"),
]

# 安全与性能上限：
#   MAX_TOOL_ROUNDS     — 一轮用户任务里，最多允许「模型调工具」多少次，防止死循环
#   MAX_READ_CHARS      — 读文件返回给模型的最大字符数，避免上下文爆掉
#   MAX_REASONING_*     — 小模型「思考」阶段有时会重复啰嗦，用来检测并打断
MAX_TOOL_ROUNDS = 48
MAX_READ_CHARS = 80_000
MAX_REASONING_CHARS = 6000
REASONING_LOOP_NGRAM = 24
REASONING_LOOP_THRESHOLD = 4
MAX_REASONING_ABORTS = 3

# 写操作 / 跑命令前，需要用户在终端确认（读文件等安全操作直接放行）。
# AUTO_APPROVE        — 本轮任务内自动放行（选「自动执行」或下一轮会重置）
# AUTO_APPROVE_ALWAYS — 全局自动（用户输入 /auto），直到 /manual
CONFIRM_TOOLS = {
    "write_file",
    "write_files",
    "edit_file",
    "multi_edit",
    "replace_lines",
    "insert_lines",
    "delete_lines",
    "delete_file",
    "delete_files",
    "move_file",
    "run_command",
    "kill_process",
}
AUTO_APPROVE = False
AUTO_APPROVE_ALWAYS = False

# run_command 命令一旦含这些符号就不算「一望而知只读」，仍需确认——
# 防止用安全命令开头、后面拼接删改操作（如 "dir & del file" / "cat a > b"）。
_DANGEROUS_CMD_TOKENS_RE = re.compile(r"[|;&`]|\$\(|<|>")

# 明确只读、不改动任何东西的查看类命令前缀：run_command 里跑这些可以免确认，
# 避免「查文件在不在/看内容」这类只读检查还要用户手动点一下。
SAFE_READONLY_CMD_PREFIXES = (
    "dir", "ls", "ll", "type", "cat", "more", "less", "tree", "where", "which",
    "pwd", "whoami", "hostname", "ver", "date", "time",
    "git status", "git diff", "git log", "git branch", "git show", "git remote",
    "node -v", "node --version", "python -v", "python -V", "python --version",
    "python3 --version", "npm -v", "npm --version", "npm list", "npm ls",
    "pip show", "pip list", "pip --version", "pip3 --version",
)

def is_safe_readonly_command(command: str) -> bool:
    """判断 run_command 里的命令是否明显只读（查看/确认类），可以免用户确认。

    要求：不含管道/重定向/命令链接等符号，且命令以已知只读命令开头。
    只做保守白名单匹配，不识别的一律走原来的确认流程。
    """
    cmd = (command or "").strip()
    if not cmd or _DANGEROUS_CMD_TOKENS_RE.search(cmd):
        return False
    low = cmd.lower()
    return any(low == p or low.startswith(p + " ") for p in SAFE_READONLY_CMD_PREFIXES)
