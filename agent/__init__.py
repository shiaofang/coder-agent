"""本地终端 AI 编程助手（agent 包）。

模块分工：
  config          — 配置（config.json）、斜杠命令、安全开关、运行时状态
  server          — 托管本地 llama-server：选模型 / 启动 / 切换 / 读 props
  prompts         — 系统提示词
  project_context — 扫描 cwd 注入项目上下文
  tools_schema    — 给模型看的工具说明书
  tools           — tool_xxx 实现 + execute_tool
  model           — 与模型服务通信 / chat_once / 统计
  context         — 上下文用量估算与压缩
  render          — rich 渲染：markdown 流式回复、diff、工具结果、统计
  terminal        — 按键读取、确认菜单、prompt_toolkit 输入
  session         — 会话落盘 / 恢复
  paths           — Windows 路径解析
  loop            — run_agent_turn 多轮工具循环
  main            — 程序入口

推荐阅读顺序：
  main → loop → model → tools → 任意一个 tool_xxx
"""
