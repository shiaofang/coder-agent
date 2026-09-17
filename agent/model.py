"""与模型服务通信：流式 chat completions、统计信息、纯文本请求（用于压缩摘要）。

llama-server 提供 OpenAI 风格接口：
  GET  /health              — 是否就绪
  POST /v1/chat/completions — 聊天（本程序用 stream=True 流式接收）

chat_once = 问模型一次（可能得到文字，也可能得到 tool_calls）。
下一步可读：agent.loop（多轮工具循环）。
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field

from agent import config
from agent.config import (
    MAX_REASONING_CHARS,
    REASONING_LOOP_NGRAM,
    REASONING_LOOP_THRESHOLD,
)
from agent.render import StreamRenderer, console, warn
from agent.tools_schema import get_tools


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    reasoning: str = ""
    looped: bool = False
    # 服务端统计：prompt_n / predicted_n / predicted_per_second（llama-server timings）
    stats: dict = field(default_factory=dict)


def _auth_headers(base: dict[str, str]) -> dict[str, str]:
    """云端模式下附带 Authorization: Bearer <api_key>；本地模式不加。"""
    if config.PROVIDER == "cloud" and config.API_KEY:
        return {**base, "Authorization": f"Bearer {config.API_KEY}"}
    return base


def request_json(method: str, path: str, body: dict | None = None, timeout: float = 600.0):
    """向模型服务发 HTTP 请求，解析返回的 JSON。"""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{config.BASE}{path}",
        data=data,
        method=method,
        headers=_auth_headers({"Content-Type": "application/json", "Accept": "application/json"}),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_ready(retries: int = 120) -> bool:
    """轮询 /health 等模型服务就绪（本地由 server.py 负责启动；云端直接视为就绪）。"""
    if config.PROVIDER == "cloud":
        return True
    with console.status("[dim]等待模型服务…[/]", spinner="dots"):
        for _ in range(retries):
            try:
                request_json("GET", "/health", timeout=2)
                return True
            except Exception:
                time.sleep(0.45)
    return False


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


def _base_payload(messages: list[dict]) -> dict:
    payload: dict = {"messages": messages, "stream": True}
    for key, val in config.SAMPLING.items():
        if val is not None:
            payload[key] = val
    if config.MODEL_NAME and config.PROVIDER == "cloud":
        payload["model"] = config.MODEL_NAME
    if not config.THINKING:
        # Qwen3 系列等：通过 chat template 关闭思考
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def _extract_stats(obj: dict, stats: dict) -> None:
    timings = obj.get("timings")
    if isinstance(timings, dict):
        for key in ("prompt_n", "predicted_n", "predicted_per_second", "prompt_per_second", "predicted_ms", "prompt_ms"):
            if timings.get(key) is not None:
                stats[key] = timings[key]
    # usage.prompt_tokens 是整段 prompt 的 token 数；timings.prompt_n 在命中 KV cache 时
    # 只算新处理的部分，所以上下文估算要优先用 prompt_tokens。
    usage = obj.get("usage")
    if isinstance(usage, dict):
        if usage.get("prompt_tokens") is not None:
            stats["prompt_tokens"] = usage["prompt_tokens"]
        if usage.get("completion_tokens") is not None and "predicted_n" not in stats:
            stats["predicted_n"] = usage["completion_tokens"]


def chat_once(messages: list[dict]) -> ChatResult:
    """One model turn: 流式渲染到终端，返回内容 / tool_calls / 思考 / 统计。"""
    payload = _base_payload(messages)
    payload["tools"] = get_tools()
    payload["tool_choice"] = "auto"
    payload["stream_options"] = {"include_usage": True}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{config.BASE}/v1/chat/completions",
        data=data,
        method="POST",
        headers=_auth_headers({"Content-Type": "application/json", "Accept": "text/event-stream"}),
    )

    result = ChatResult()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_acc: dict[int, dict] = {}
    reasoning_len_at_last_check = 0

    renderer = StreamRenderer()
    renderer.start()
    resp = None
    try:
        resp = urllib.request.urlopen(req, timeout=600)
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

            _extract_stats(obj, result.stats)
            choices = obj.get("choices") or [{}]
            delta = (choices[0] if choices else {}).get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            content = delta.get("content")
            tool_calls = delta.get("tool_calls")

            if reasoning:
                renderer.on_reasoning(reasoning)
                reasoning_parts.append(reasoning)
                total_reasoning_len = sum(len(r) for r in reasoning_parts)
                if total_reasoning_len > MAX_REASONING_CHARS:
                    result.looped = True
                elif total_reasoning_len - reasoning_len_at_last_check >= 150:
                    reasoning_len_at_last_check = total_reasoning_len
                    if _detect_reasoning_loop("".join(reasoning_parts)):
                        result.looped = True
                if result.looped:
                    renderer.abort()
                    warn("检测到重复思考循环，已中断本次生成")
                    break

            if content:
                renderer.on_content(content)
                content_parts.append(content)

            if tool_calls:
                renderer.on_tool_calls()
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
    except BaseException:
        renderer.abort()
        raise
    finally:
        # 断开连接让 llama-server 立刻停止生成、释放 slot
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    renderer.finish()

    tools = [tool_acc[i] for i in sorted(tool_acc)]
    tools = [t for t in tools if t["function"]["name"]]
    if result.looped:
        # 循环时工具调用大概率不完整/无意义，丢弃，交给上层用提示重试
        tools = []
    result.content = "".join(content_parts)
    result.tool_calls = tools
    result.reasoning = "".join(reasoning_parts)
    return result


def chat_plain(messages: list[dict], max_tokens: int = 1200) -> str:
    """不带工具、不渲染的一次请求（上下文压缩摘要用）。返回纯文本。"""
    payload = _base_payload(messages)
    payload["stream"] = False
    payload["max_tokens"] = max_tokens
    payload["chat_template_kwargs"] = {"enable_thinking": False}
    data = request_json("POST", "/v1/chat/completions", payload, timeout=600)
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return str(msg.get("content") or "").strip()
