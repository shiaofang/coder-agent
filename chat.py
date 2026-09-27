#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cursor 本地模型桥接入口。

选 models/ 下的 GGUF → 启动 bin/llama-server → 提示 Cloudflare 隧道，
供 Cursor 自定义模型使用。实现见 agent/。
"""

from __future__ import annotations

from agent.main import main

if __name__ == "__main__":
    raise SystemExit(main())
