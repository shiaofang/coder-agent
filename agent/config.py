"""配置常量：本地 GGUF / llama-server 参数、采样参数、搜索 Key、斜杠命令、安全上限、会话开关。

运行时配置统一放在项目根目录的 config.json（已 gitignore）。
模板见 config.example.json。会话开关（AUTO_APPROVE / THINKING / VERBOSE 等）
放在本模块，其它文件用 `import agent.config as config` 再读写。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT_DIR / "bin"
MODEL_DIR = ROOT_DIR / "models"
SESSION_DIR = ROOT_DIR / "sessions"
HISTORY_FILE = ROOT_DIR / ".history"
LAST_MODEL_FILE = ROOT_DIR / ".last_model"

# ========================================================================
#  默认值（可被 config.json / 环境变量覆盖）
# ========================================================================
HOST = "127.0.0.1"
PORT = 8080
BASE = f"http://{HOST}:{PORT}"

# CONFIG_ERROR — 配置缺失/不完整时的错误说明，main.py 启动时检查并退出
TAVILY_API_KEY = ""
CONFIG_ERROR: str | None = None

# llama-server 启动参数（config.json -> "server"）
#   fit_margin  — 预留给桌面/浏览器的显存 MiB（-fitt）
#   fit_ctx     — --fit 允许的最小上下文（-fitc）；系统提示 + 工具声明约 2k token
#   ngl / ctx   — 手动写死 -ngl / -c（None = 交给 --fit 自适应）
#   extra_args  — 追加的原始参数列表
SERVER: dict = {
    "fit_margin": 384,
    "fit_ctx": 16384,
    "ngl": None,
    "ctx": None,
    "extra_args": [],
}

# 采样参数（config.json -> "sampling"），非 None 的字段才会放进请求体
SAMPLING: dict = {
    "temperature": 0.3,
    "top_p": None,
    "top_k": None,
    "min_p": None,
    "repeat_penalty": None,
}

CONFIG_PATH = ROOT_DIR / "config.json"


def _load_config() -> None:
    """读取 config.json，并用环境变量覆盖（搜索 Key / NGL / CTX）。

    优先级：
      搜索 Key — 环境变量 TAVILY_API_KEY > config.json tavily_api_key
      ngl/ctx  — 环境变量 CODER_AGENT_NGL / CODER_AGENT_CTX > config.json server.*
    """
    global HOST, PORT, BASE, TAVILY_API_KEY, CONFIG_ERROR

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

    # —— 本地服务地址 ——
    if data.get("host") not in (None, ""):
        HOST = str(data["host"]).strip()
    if data.get("port") not in (None, ""):
        try:
            PORT = int(data["port"])
        except (TypeError, ValueError):
            CONFIG_ERROR = "config.json 的 port 必须是整数"
            return

    # —— 搜索 Key（环境变量优先）——
    TAVILY_API_KEY = (
        os.environ.get("TAVILY_API_KEY", "").strip()
        or str(data.get("tavily_api_key", "")).strip()
    )

    # —— llama-server 参数 ——
    srv = data.get("server")
    if isinstance(srv, dict):
        for key in ("fit_margin", "fit_ctx", "ngl", "ctx"):
            if key in srv and srv[key] not in (None, ""):
                try:
                    SERVER[key] = int(srv[key])
                except (TypeError, ValueError):
                    CONFIG_ERROR = f"config.json 的 server.{key} 必须是整数"
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

    # —— 采样参数 ——
    samp = data.get("sampling")
    if isinstance(samp, dict):
        for key in SAMPLING:
            if key in samp and samp[key] not in (None, ""):
                try:
                    SAMPLING[key] = float(samp[key]) if key != "top_k" else int(samp[key])
                except (TypeError, ValueError):
                    CONFIG_ERROR = f"config.json 的 sampling.{key} 必须是数字"
                    return

    BASE = f"http://{HOST}:{PORT}"


_load_config()


# ========================================================================
#  运行时状态（会话内可变）
# ========================================================================
# 当前模型信息（server.py 启动后填写）
MODEL_LABEL = ""  # 横幅/状态栏显示用
MODEL_PARAMS_B: float | None = None  # 从文件名解析的参数量（B）
MODEL_N_CTX = 0  # 服务端实际上下文长度；0 = 未知
# 本地模型的思考深度（--reasoning-effort），如 "low"/"medium"/"xhigh"；
# 空串 = 不传，用模型模板自带的默认档。选模型时按模板支持情况询问后写入。
REASONING_EFFORT = ""

# 会话开关
THINKING = True  # /think on|off → chat_template_kwargs.enable_thinking
VERBOSE = False  # /verbose → 工具结果 / 思考全量显示


def n_ctx() -> int:
    """当前上下文长度（未知时用 server.fit_ctx）。"""
    if MODEL_N_CTX > 0:
        return MODEL_N_CTX
    return int(SERVER["fit_ctx"])


# 用户可输入的斜杠命令（不区分大小写，在 main 里处理）
EXIT_CMDS = {"/exit", "/quit", "/q", "exit", "quit"}
CLEAR_CMDS = {"/clear_cache", "/reset", "/new", "/clear"}
AUTO_CMDS = {"/auto"}
MANUAL_CMDS = {"/manual"}
PWD_CMDS = {"/pwd", "/dir"}
CD_CMD = "/cd"  # 后面带参数（路径），不能放进固定集合，用前缀匹配
MODEL_CMDS = {"/model"}
COMPACT_CMDS = {"/compact"}
CTX_CMDS = {"/ctx"}
RESUME_CMD = "/resume"  # 可带序号参数
SESSIONS_CMDS = {"/sessions"}
VERBOSE_CMDS = {"/verbose"}
THINK_CMD = "/think"  # /think on|off
HELP_CMDS = {"/help", "/?"}

# 输入以 / 开头时弹出的命令菜单（展示用；实际匹配仍看上面的集合）
SLASH_MENU: list[tuple[str, str]] = [
    ("/cd", "切换工作目录，如 /cd .. 或 /cd D:\\project"),
    ("/pwd", "查看当前工作目录"),
    ("/model", "切换本地 GGUF 模型（保留对话）"),
    ("/compact", "压缩对话历史，腾出上下文"),
    ("/ctx", "查看上下文用量"),
    ("/think", "/think on|off 开关模型思考"),
    ("/verbose", "切换工具结果 / 思考全量显示"),
    ("/resume", "恢复最近的会话，/resume 2 选第 2 条"),
    ("/sessions", "列出已保存的会话"),
    ("/new", "新会话（清空对话）"),
    ("/auto", "全程自动执行"),
    ("/manual", "每次确认"),
    ("/help", "命令说明"),
    ("/exit", "退出"),
]

# 安全与性能上限：
#   MAX_TOOL_ROUNDS     — 一轮用户任务里，最多允许「模型调工具」多少次，防止死循环
#   MAX_READ_CHARS      — 读文件返回给模型的最大字符数，避免上下文爆掉
#   MAX_REASONING_*     — 思考末尾连续自我复制时打断；长度只做 runaway 上限
MAX_TOOL_ROUNDS = 48
MAX_READ_CHARS = 80_000
MAX_REASONING_CHARS = 24_000
REASONING_LOOP_MIN_UNIT = 40
REASONING_LOOP_REPEATS = 5
MAX_REASONING_ABORTS = 3
MAX_EMPTY_RESPONSE_RETRIES = 2

# 上下文压缩阈值（占 n_ctx 的比例）
COMPACT_L1_RATIO = 0.75  # 折叠旧工具结果
COMPACT_L2_RATIO = 0.90  # 模型总结旧对话

# 写操作 / 跑命令前需要用户确认；普通只读检查（含无头浏览器检查）自动放行。
# AUTO_APPROVE        — 本轮任务内自动放行（选「自动执行」或下一轮会重置）
# AUTO_APPROVE_ALWAYS — 全局自动（用户输入 /auto），直到 /manual
CONFIRM_TOOLS = {
    "write_file",
    "edit_file",
    "edit_lines",
    "delete_path",
    "move_file",
    "run_command",
    "process",
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
