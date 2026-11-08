#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 GGUF 模型 API 入口。

选 models/ 下的 GGUF → 启动 bin/llama-server → 打印本机接口与隧道提示，
让任何 OpenAI 兼容客户端都能调用。实现见 agent/。
"""

from __future__ import annotations

from agent.main import main

if __name__ == "__main__":
    raise SystemExit(main())
