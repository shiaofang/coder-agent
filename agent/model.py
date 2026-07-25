"""与本地模型服务通信：health 探测、流式 chat completions。

llama-server 提供 OpenAI 风格接口：
  GET  /health              — 是否就绪
  POST /v1/chat/completions — 聊天（本程序用 stream=True 流式接收）

chat_once = 问模型一次（可能得到文字，也可能得到 tool_calls）。
下一步可读：agent.loop（多轮工具循环）。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

from agent.config import (
    BASE,
    MAX_REASONING_CHARS,
    REASONING_LOOP_NGRAM,
    REASONING_LOOP_THRESHOLD,
)
from agent.terminal import C, paint, write_stream
from agent.tools_schema import TOOLS

# ========================================================================
#  第 8 区：与本地模型服务通信

# ========================================================================
# llama-server 提供 OpenAI 风格接口：
#   GET  /health              — 是否就绪
#   GET  /v1/models           — 当前加载的模型
#   POST /v1/chat/completions — 聊天（本程序用 stream=True 流式接收）
#

def request_json(method: str, path: str, body: dict | None = None, timeout: float = 600.0):
    """向本地 llama-server 发 HTTP 请求，解析返回的 JSON。"""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def wait_ready(retries: int = 120) -> bool:
    """轮询 /health，等模型服务就绪；带加载动画。"""
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    for i in range(retries):
        try:
            request_json("GET", "/health", timeout=2)
            sys.stdout.write("\r" + " " * 48 + "\r")
            sys.stdout.flush()
            return True
        except Exception:
            frame = frames[i % len(frames)]
            sys.stdout.write(
                f"\r{paint('✻', C.SPINNER)} {paint(frame + ' Loading model…', C.SPINNER_LABEL)}"
            )
            sys.stdout.flush()
            time.sleep(0.45)
    return False

# ========================================================================
#  第 9 区：Agent 循环（核心逻辑）

# ========================================================================
# chat_once  = 问模型一次（可能得到文字，也可能得到 tool_calls）
# run_agent_turn = 若有工具就执行，把结果塞回 messages，再问，直到最终回答
#

def _detect_reasoning_loop(
    text: str,
    ngram: int = REASONING_LOOP_NGRAM,
    threshold: int = REASONING_LOOP_THRESHOLD,
) -> bool:
    """Cheap n-gram repetition detector for degenerate 'thinking' loops."""
    if len(text) < ngram * threshold:
        return False
    counts: dict[str, int] = {}
    step = max(1, ngram // 2)
    for i in range(0, len(text) - ngram, step):
        gram = text[i : i + ngram]
        if not gram.strip():
            continue
        n = counts.get(gram, 0) + 1
        counts[gram] = n
        if n >= threshold:
            return True
    return False

def chat_once(messages: list[dict]) -> tuple[str, list[dict], str, bool]:
    """One model turn. Returns (content, tool_calls, reasoning, looped)."""
    # 发给本地模型的请求体：历史消息 + 工具说明书 + 流式输出
    payload = {
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": True,
        "temperature": 0.3,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_acc: dict[int, dict] = {}
    thinking = False
    replied = False
    started = False
    looped = False
    reasoning_len_at_last_check = 0

    sys.stdout.write(paint("✻", C.SPINNER) + " " + paint("Working…", C.SPINNER_LABEL))
    sys.stdout.flush()

    def clear_spinner() -> None:
        nonlocal started
        if not started:
            sys.stdout.write("\r" + " " * 28 + "\r")
            sys.stdout.flush()
            started = True

    with urllib.request.urlopen(req, timeout=600) as resp:
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue

            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            content = delta.get("content")
            tool_calls = delta.get("tool_calls")

            if reasoning:
                clear_spinner()
                if not thinking:
                    thinking = True
                    print(paint("∴", C.THINK_ICON) + " " + paint("Thinking…", C.DIM, C.SPINNER_LABEL))
                    sys.stdout.write("  " + C.THINK_TEXT)
                    sys.stdout.flush()
                write_stream(reasoning, C.THINK_TEXT, indent="  ")
                reasoning_parts.append(reasoning)

                total_reasoning_len = sum(len(r) for r in reasoning_parts)
                if total_reasoning_len > MAX_REASONING_CHARS:
                    looped = True
                elif total_reasoning_len - reasoning_len_at_last_check >= 150:
                    reasoning_len_at_last_check = total_reasoning_len
                    if _detect_reasoning_loop("".join(reasoning_parts)):
                        looped = True

                if looped:
                    sys.stdout.write(C.RESET + "\n")
                    print(
                        paint("⚠ 检测到重复思考循环，已中断本次生成", C.ERR)
                    )
                    try:
                        resp.close()
                    except Exception:
                        pass
                    break

            if content:
                clear_spinner()
                if thinking:
                    sys.stdout.write(C.RESET + "\n\n")
                    thinking = False
                if not replied:
                    sys.stdout.write(paint("⏺", C.REPLY_ICON) + " " + C.REPLY)
                    sys.stdout.flush()
                    replied = True
                write_stream(content, C.REPLY, indent="")
                content_parts.append(content)

            if tool_calls:
                clear_spinner()
                if thinking:
                    sys.stdout.write(C.RESET + "\n")
                    thinking = False
                for tc in tool_calls:
                    idx = tc.get("index", 0)
                    slot = tool_acc.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if tc.get("type"):
                        slot["type"] = tc["type"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

    clear_spinner()
    if (thinking or replied) and not looped:
        sys.stdout.write(C.RESET + "\n")

    tools = [tool_acc[i] for i in sorted(tool_acc)]
    # drop empty tool stubs
    tools = [t for t in tools if t["function"]["name"]]
    if looped:
        # 循环时工具调用大概率不完整/无意义，丢弃，交给上层用提示重试
        tools = []
    return "".join(content_parts), tools, "".join(reasoning_parts), looped

