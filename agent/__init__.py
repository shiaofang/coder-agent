"""本地 GGUF → llama-server → Cursor 自定义模型桥接。

模块分工：
  config         — config.json、路径、运行时模型状态
  server         — 选模型 / 端口清理 / 启动 / 停止 llama-server
  render         — rich 终端输出
  process_guard  — 关窗 / 强杀时带走 llama-server
  main           — 程序入口（打印隧道提示并保活）
"""
