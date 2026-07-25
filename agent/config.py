"""配置常量：本地模型地址、斜杠命令、安全上限、写操作确认开关。

改 HOST/PORT 时，要保证和 start.bat 里启动 llama-server 的参数一致。
会话开关 AUTO_APPROVE / AUTO_APPROVE_ALWAYS 放在本模块，其它文件用
`import agent.config as config` 再读写 `config.AUTO_APPROVE`。
"""

from __future__ import annotations

import re

HOST = "127.0.0.1"
PORT = 8080
BASE = f"http://{HOST}:{PORT}"

# 用户可输入的斜杠命令（不区分大小写，在 main 里处理）
EXIT_CMDS = {"/exit", "/quit", "/q", "exit", "quit"}
CLEAR_CMDS = {"/clear_cache", "/reset", "/new"}
AUTO_CMDS = {"/auto"}
MANUAL_CMDS = {"/manual"}

# 输入以 / 开头时弹出的命令菜单（展示用；实际匹配仍看上面的集合）
SLASH_MENU: list[tuple[str, str]] = [
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
