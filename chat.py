#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地终端 AI 编程助手（chat.py）
================================

【这个程序在干什么？】
  start.bat 会先启动 bin/llama-server.exe（本地大模型服务），
  再运行本文件。本文件是一个「终端聊天客户端 + 工具执行器」：

  1. 你在终端里打字（例如：帮我在某某目录创建一个 Vue 项目）
  2. 本程序把对话发给本地模型（http://127.0.0.1:8080）
  3. 模型可以「调用工具」（读文件、改文件、跑命令、搜网页…）
  4. 本程序真正执行这些工具，把结果再发给模型
  5. 模型根据结果继续思考，直到给出最终中文回复

【为什么没有 LangChain / @tool？】
  这是故意手写的「OpenAI 兼容 tool calling」：
  - TOOLS 列表 = 告诉模型「你有哪些工具、参数长什么样」
  - execute_tool() = 模型说完要调用后，真正去执行
  不依赖第三方 Agent 框架，装好 Python 就能跑。

【代码在哪里？】
  实现已拆到 agent/ 包，本文件只做启动入口：

  agent/config.py        — 地址、命令、安全开关
  agent/prompts.py       — 系统提示词
  agent/tools_schema.py  — 给模型看的工具说明书（JSON Schema）
  agent/terminal.py      — 颜色、横幅、确认菜单、输入
  agent/paths.py         — Windows 路径解析
  agent/tools.py         — tool_xxx 真正干活 + execute_tool 分发
  agent/model.py         — 跟 llama-server 通信 / chat_once
  agent/loop.py          — 多轮「思考 → 用工具 → 再思考」
  agent/main.py          — main() 主循环

【推荐阅读顺序（初学者）】
  agent/main.py → loop.py → model.py → tools.py → 任意一个 tool_xxx
"""

from __future__ import annotations

from agent.main import main

# Python 惯例：只有「python chat.py」直接运行时，__name__ 才等于 "__main__"。
# raise SystemExit(main())：用 main 的返回值作为进程退出码（给 start.bat 用）。
if __name__ == "__main__":
    raise SystemExit(main())
