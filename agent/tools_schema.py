"""工具声明 TOOLS：给模型看的说明书（OpenAI function calling JSON Schema）。

这里只是「声明」。真正执行在 agent.tools 的 tool_xxx / execute_tool。
新增工具时要改两处：TOOLS 声明 + agent.tools 里的 tool_xxx 实现
（execute_tool 会按 tool_ 前缀自动注册，无需手动加分支）。
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
            "name": "write_files",
            "description": (
                "一次性创建/覆盖写入多个文件，适合「批量创建 N 个文件」的场景，"
                "比逐个调用 write_file 更省事、也少弹几次确认。每条同 write_file 语义。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "description": "要写入的文件列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                                "content": {"type": "string", "description": "文件内容，可为空字符串"},
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["files"],
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
            "name": "multi_edit",
            "description": (
                "一次性对多个文件/多处内容做精确替换（每条 = edit_file 的一次调用），"
                "适合跨文件重构、批量改一个函数签名的所有调用点。逐条执行，某条失败不影响其它条，"
                "返回里会写清第几条成功/失败。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "编辑列表，按顺序依次执行",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": f"文件路径。{PATH_HINT}"},
                                "old_text": {"type": "string", "description": "要被替换的原文（需足够独特）"},
                                "new_text": {"type": "string", "description": "替换后的新文本"},
                                "replace_all": {
                                    "type": "boolean",
                                    "description": "是否替换该文件内所有匹配，默认 false",
                                },
                            },
                            "required": ["path", "old_text", "new_text"],
                        },
                    },
                },
                "required": ["edits"],
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
            "name": "delete_files",
            "description": "一次性删除多个文件，适合「批量删除」的场景，比逐个 delete_file 更省事。",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "description": "要删除的文件路径列表",
                        "items": {"type": "string"},
                    },
                },
                "required": ["paths"],
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
            "name": "list_processes",
            "description": "列出本次会话里通过 run_command 启动的后台进程（比如 dev server），含 pid/状态/日志路径。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_process_output",
            "description": "读取某个后台进程（pid 来自 run_command 或 list_processes）的日志输出，可只看最后 N 行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "进程 pid"},
                    "tail_lines": {"type": "integer", "description": "只看最后 N 行，可选，默认全部"},
                },
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_process",
            "description": "结束某个后台进程及其子进程（比如换端口前先杀掉占用中的旧 dev server）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "进程 pid"},
                },
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_syntax",
            "description": (
                "对单个文件做快速语法自检（.py/.json/.js/.jsx/.mjs/.cjs），"
                "改完代码后可先用它排除低级语法错误，再跑真正的构建/测试；不支持的语言用 run_command。"
            ),
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
            "name": "web_search",
            "description": (
                "联网搜索（Tavily；查报错、API、官方文档）。"
                "同一 build/lint 错误修两次仍失败时必须调用，查询应包含完整报错关键词。"
                "结果含标题、链接与摘要；需要正文时再 fetch_url。"
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
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "创建或更新本会话的任务计划清单。多步骤任务开始时先调用一次写出全部步骤；"
                "每完成一步再 merge 更新对应项的 status。"
                "每项必须有 content（步骤说明）和 status；id 建议用 1/2/3。"
                "示例参数："
                '{"todos":[{"id":"1","content":"创建目录","status":"in_progress"},'
                '{"id":"2","content":"写 hello.txt","status":"pending"}],"merge":false}'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": (
                            "todo 对象数组。每项字段：id（字符串）、content（步骤说明，必填）、"
                            "status（pending|in_progress|completed|cancelled）。"
                            "也接受 task/title 作为 content 的别名。"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "稳定 id，如 \"1\"、\"2\"、\"setup\"",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "这一步要做什么（简短中文/英文均可）",
                                },
                                "status": {
                                    "type": "string",
                                    "description": "pending | in_progress | completed | cancelled",
                                },
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                    "merge": {
                        "type": "boolean",
                        "description": (
                            "false=整表替换（第一次建计划用）；"
                            "true=按 id 合并更新（默认；推进进度时用，可只传 id+status）"
                        ),
                    },
                },
                "required": ["todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_read",
            "description": "读取当前会话的任务计划清单（进度回顾时用）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
