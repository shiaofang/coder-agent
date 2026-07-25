"""工具声明 TOOLS：给模型看的说明书（OpenAI function calling JSON Schema）。

这里只是「声明」。真正执行在 agent.tools 的 tool_xxx / execute_tool。
新增工具时要改三处：TOOLS + tool_xxx 实现 + execute_tool 分支。
"""

from __future__ import annotations

PATH_HINT = "优先使用用户给出的绝对路径（如 C:\\\\Users\\\\...），不要擅自改成相对路径"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取文本文件；默认带行号。可指定 start_line/end_line 只读片段，"
                "便于配合 replace_lines / delete_lines。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "start_line": {"type": "integer", "description": "起始行（1-based），可选"},
                    "end_line": {"type": "integer", "description": "结束行（含），可选"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "创建新文件或在必须整文件重写时覆盖写入。"
                "已有文件的小改动禁止用本工具，请用 edit_file / replace_lines / insert_lines / delete_lines。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "精确文本替换（改几个字符/一句话首选）。old_text→new_text；"
                "默认只替换第一处，replace_all=true 时替换全部。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "old_text": {"type": "string", "description": "要被替换的原文（需足够独特）"},
                    "new_text": {"type": "string", "description": "替换后的新文本，可为空表示删除该段"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有匹配，默认 false",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_lines",
            "description": "按行号替换一段代码（1-based，含首尾）。适合改连续几行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "start_line": {"type": "integer", "description": "起始行号（从1开始）"},
                    "end_line": {"type": "integer", "description": "结束行号（含）"},
                    "new_content": {"type": "string", "description": "替换后的内容（可多行）"},
                },
                "required": ["path", "start_line", "end_line", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_lines",
            "description": "在指定行后插入代码（after_line=0 表示文件开头）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "after_line": {"type": "integer", "description": "插入到该行之后，0=文件开头"},
                    "content": {"type": "string", "description": "要插入的文本"},
                },
                "required": ["path", "after_line", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_lines",
            "description": "按行号删除一段代码（1-based，含首尾）。挪代码时配合 insert_lines 使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除文件（不是删行）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "移动或重命名文件/目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": f"源路径。{PATH_HINT}"},
                    "dest": {"type": "string", "description": f"目标路径。{PATH_HINT}"},
                },
                "required": ["src", "dest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mkdir",
            "description": "创建目录（含中间目录）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"目录路径。{PATH_HINT}"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认当前目录"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "按 glob 模式查找文件，例如 **/*.js",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "root": {"type": "string", "description": "搜索根目录，默认当前目录"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "在文件中搜索文本（正则），返回 path:line:content",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "文件或目录"},
                    "glob": {"type": "string", "description": "可选，限制文件名如 *.py"},
                },
                "required": ["pattern", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在 shell 中执行命令。短命令前台等待；"
                "npm run dev / vite 等常驻服务会自动后台启动并返回启动日志与访问地址。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "工作目录，可选"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索（查报错、API、官方文档）。"
                "同一 build/lint 错误修两次仍失败时必须调用，查询应包含完整报错关键词。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，建议带上完整错误信息"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "抓取网页正文（文档/StackOverflow/GitHub issue），配合 web_search 使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http/https URL"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_datetime",
            "description": "获取当前本地日期时间",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
