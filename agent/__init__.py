"""本地终端 AI 编程助手（agent 包）。

模块分工：
  config        — 地址、命令、安全开关
  prompts       — 系统提示词
  tools_schema  — 给模型看的工具说明书
  terminal      — 颜色、横幅、确认菜单、输入
  paths         — Windows 路径解析
  tools         — tool_xxx 实现 + execute_tool
  model         — 与 llama-server 通信 / chat_once
  loop          — run_agent_turn 多轮工具循环
  main          — 程序入口

推荐阅读顺序：
  main → loop → model → tools → 任意一个 tool_xxx
"""
