"""系统提示词：角色、路径与验收契约。工具怎么用见 agent.tools_schema。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from agent import config
from agent.project_context import gather_project_context

SYSTEM_PROMPT = """
你是编程助手。用户要求实现/修改/修复时，用工具直接改文件、跑命令并验证，不要只给步骤；只要求检查/解释/诊断时，只读调查并报告依据，未要求修复就不要改文件。不要提交无变化或无依据的修改。完成后用简短中文总结结论或改动与验证结果，停止调用工具。

路径：用户给的绝对路径原样传给工具。【项目上下文】只供定向，改代码前以工具读到的真实内容为准。其中的 AGENTS.md / .cursorrules 等规则必须遵守。

局部修改用 edit_file 或 edit_lines；新建或结构性重写用 write_file。常驻服务会自动转后台并返回 pid，用 process 读日志/结束。

验收：check_syntax → 项目已声明的 lint/typecheck/test/build → 前端用 check_webpage。总结写清跑了什么、没跑什么及原因。需求不清先读代码；确实无法判断再一次问清。
"""

_WEB_HINT = "\n联网：查报错、API 或文档时用 web_search，再 fetch_url 打开相关页面。"


def build_system_prompt() -> str:
    """组装发给模型的系统提示：通用规则 + 运行环境 + 项目上下文（上限随 n_ctx 缩放）。"""
    cwd = Path.cwd()
    base = SYSTEM_PROMPT
    if config.TAVILY_API_KEY:
        base = base.rstrip() + _WEB_HINT + "\n"
    parts = [
        base,
        f"\n【运行环境】\n当前工作目录（cwd）= {cwd}\n"
        f"当前日期 = {datetime.now().strftime('%Y-%m-%d %A')}\n"
        "相对路径会解析到上述 cwd。用户消息里的绝对路径请完整复制到工具参数。",
    ]
    ctx = gather_project_context(cwd, max_chars=project_context_budget())
    if ctx:
        parts.append("\n\n【项目上下文】\n" + ctx)
    return "".join(parts)


def project_context_budget() -> int:
    """项目上下文允许的字符数：16k 上下文约 4k 字符，32k 及以上 7k。"""
    n = config.n_ctx()
    if n <= 8_192:
        return 2_000
    if n <= 16_384:
        return 4_000
    if n <= 24_576:
        return 5_500
    return 7_000
