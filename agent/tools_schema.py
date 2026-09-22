"""工具声明 TOOLS：给模型看的说明书（OpenAI function calling JSON Schema）。

这里只是「声明」。真正执行在 agent.tools 的 tool_xxx / execute_tool。
新增工具时要改两处：TOOLS 声明 + agent.tools 里的 tool_xxx 实现
（execute_tool 会按 tool_ 前缀自动注册，无需手动加分支）。

描述刻意写短：本地小模型上下文有限，整套声明控制在 ~1.5k token 以内。
web_search / fetch_url 只在配置了 TAVILY_API_KEY 时才发给模型（get_tools）。
"""

from __future__ import annotations

from agent import config

_PATH = {"type": "string", "description": "文件路径；用户给的绝对路径原样使用"}


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    params: dict = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


TOOLS: list[dict] = [
    _fn(
        "read_file",
        "读文件，带行号。大文件优先用 start_line/end_line 读相关片段；要看多个文件时传 paths 一次读完。",
        {
            "path": _PATH,
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "paths": {
                "type": "array",
                "description": "批量：一次读多个文件，各读整篇，最多 10 个",
                "items": _PATH,
            },
        },
    ),
    _fn(
        "write_file",
        "新建或整文件覆盖。已有文件的小改动用 edit_file。批量新建传 files。",
        {
            "path": _PATH,
            "content": {"type": "string"},
            "files": {
                "type": "array",
                "description": "批量：[{path, content}, …]",
                "items": {
                    "type": "object",
                    "properties": {"path": _PATH, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
        },
    ),
    _fn(
        "edit_file",
        "精确替换 old_text→new_text（两者必须不同）。仅在确有修改时调用；多处/多文件传 edits。",
        {
            "path": _PATH,
            "old_text": {"type": "string", "description": "原文，需唯一"},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean", "description": "替换全部匹配，默认 false"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": _PATH,
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        },
    ),
    _fn(
        "edit_lines",
        "按行号编辑（1-based 含首尾）。mode=replace 替换 start..end；insert 插到 start_line 之后（0=开头）；delete 删除 start..end。",
        {
            "path": _PATH,
            "mode": {"type": "string", "enum": ["replace", "insert", "delete"]},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "content": {"type": "string", "description": "replace/insert 的新内容"},
        },
        ["path", "mode", "start_line"],
    ),
    _fn(
        "delete_path",
        "删除文件或空目录，可传多个。",
        {"paths": {"type": "array", "items": {"type": "string"}}},
        ["paths"],
    ),
    _fn(
        "move_file",
        "移动/重命名文件或目录。",
        {"src": _PATH, "dest": _PATH},
        ["src", "dest"],
    ),
    _fn(
        "list_dir",
        "列目录。结果中 [FILE]/[DIR] 是类型标记，不是文件名；调用后续工具时只复制标记后面的完整路径。",
        {"path": {"type": "string", "description": "默认当前目录"}},
    ),
    _fn(
        "glob_search",
        "按 glob 找文件，如 **/*.py。",
        {"pattern": {"type": "string"}, "root": {"type": "string"}},
        ["pattern"],
    ),
    _fn(
        "grep_search",
        "正则搜文件内容，返回 path:line:content。",
        {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "文件或目录"},
            "glob": {"type": "string", "description": "限定文件名如 *.py"},
        },
        ["pattern", "path"],
    ),
    _fn(
        "run_command",
        "执行 shell 命令。项目脚本必须真实存在，验收命令不得用 || true 掩盖失败；用 cwd 指定目录。dev server 自动转后台。",
        {"command": {"type": "string"}, "cwd": {"type": "string"}},
        ["command"],
    ),
    _fn(
        "process",
        "后台进程管理：list 列出；read 读日志（pid，可 tail_lines）；kill 结束（pid）。",
        {
            "action": {"type": "string", "enum": ["list", "read", "kill"]},
            "pid": {"type": "integer"},
            "tail_lines": {"type": "integer"},
        },
        ["action"],
    ),
    _fn(
        "check_syntax",
        "文件级静态检查，支持 .html/.htm/.py/.json/.js/.jsx/.mjs/.cjs；不代替项目 build/lint/test 或浏览器验证。",
        {"path": _PATH},
        ["path"],
    ),
    _fn(
        "check_webpage",
        "用无头 Chrome/Edge 实际运行 HTML 或网页，检查 console.error、JS 异常、请求失败和 HTTP 错误；不验证视觉效果。",
        {
            "path": {"type": "string", "description": "本地 .html/.htm 完整路径；与 url 二选一"},
            "url": {"type": "string", "description": "http(s) 页面地址；与 path 二选一"},
            "wait_ms": {"type": "integer", "description": "页面加载后等待异步错误的毫秒数，默认 2000"},
        },
    ),
    _fn(
        "todo_write",
        "多步任务的计划清单。首次 merge=false 写全部步骤；之后 merge=true 只传 id+status 更新。同时仅 1 个 in_progress。返回当前清单。",
        {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    },
                    "required": ["id", "content", "status"],
                },
            },
            "merge": {"type": "boolean"},
        },
        ["todos"],
    ),
]

WEB_TOOLS: list[dict] = [
    _fn(
        "web_search",
        "联网搜索（查报错/API/文档）。同一错误修两次仍失败时用，查询带完整报错。",
        {"query": {"type": "string"}},
        ["query"],
    ),
    _fn(
        "fetch_url",
        "抓取网页正文。",
        {"url": {"type": "string"}},
        ["url"],
    ),
]


def get_tools() -> list[dict]:
    """当前应发给模型的工具列表：没配搜索 Key 就不暴露 web_search / fetch_url。"""
    if config.TAVILY_API_KEY:
        return TOOLS + WEB_TOOLS
    return list(TOOLS)


def tool_names() -> set[str]:
    return {t["function"]["name"] for t in get_tools()}
