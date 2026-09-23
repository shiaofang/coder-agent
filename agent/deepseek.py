"""DeepSeek 网页协议客户端：/api/v0/chat/completion。

请求体是单条 prompt + chat_session_id，返回 SSE 补丁流（APPEND / SET / BATCH），
没有 OpenAI 的 tools 字段。工具调用约定写进 prompt，用 DeepSeek DSML 标记收回，
再还原成 loop.py 认识的 OpenAI tool_calls（同时兼容旧 <tool_call> 标记）。

服务端按会话记住历史，所以只把「上次成功之后新出现的用户/工具消息」发出去。
本地 messages 被 /compact、/new 改写时，摘要对不上就另开会话，把全文重发。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from agent import config
from agent.tools_schema import get_tools

_DSML_TOKEN = "｜DSML｜"
_DSML_MARKER_RE = r"(?:[|｜]\s*)+DSML\s*(?:[|｜]\s*)+"
_DSML_CALL_BLOCK_NAME_RE = r"(?:tool_calls|function_calls|calls)"
_DSML_OPEN_RE = re.compile(
    rf"<{_DSML_MARKER_RE}{_DSML_CALL_BLOCK_NAME_RE}\s*>",
    re.IGNORECASE,
)
_DSML_CLOSE_RE = re.compile(
    rf"</{_DSML_MARKER_RE}{_DSML_CALL_BLOCK_NAME_RE}\s*>",
    re.IGNORECASE,
)
_DSML_ANY_TAG_RE = re.compile(rf"<\s*/?\s*{_DSML_MARKER_RE}", re.IGNORECASE)
_CALL_BLOCK_RE = re.compile(
    rf"<tool_call>\s*(?P<standard>.*?)\s*</tool_call>"
    rf"|(?P<dsml><{_DSML_MARKER_RE}{_DSML_CALL_BLOCK_NAME_RE}\s*>.*?"
    rf"</{_DSML_MARKER_RE}{_DSML_CALL_BLOCK_NAME_RE}\s*>)",
    re.DOTALL | re.IGNORECASE,
)
_DSML_INVOKE_RE = re.compile(
    rf"<{_DSML_MARKER_RE}invoke\b(?P<attrs>[^>]*)>(?P<body>.*?)"
    rf"</{_DSML_MARKER_RE}invoke\s*>",
    re.DOTALL | re.IGNORECASE,
)
_DSML_PARAMETER_RE = re.compile(
    rf"<{_DSML_MARKER_RE}parameter\b(?P<attrs>[^>]*)>(?P<value>.*?)"
    rf"</{_DSML_MARKER_RE}parameter\s*>",
    re.DOTALL | re.IGNORECASE,
)
_THINK_TYPES = {"THINK", "THINKING", "THOUGHT"}
_ANSWER_TYPES = {"RESPONSE", "REPLY"}

_TOOL_GUIDE = f"""需要调用工具时，只输出下面这种 DeepSeek DSML 标记，不要用 markdown 代码块包住。一个块里可以连续放多个 invoke。标记以外不要解释你准备调用工具。
字符串参数使用 string="true" 并直接填写原文；数字、布尔值、数组和对象使用 string="false" 并填写合法 JSON。
不需要工具时直接用中文回答，不要输出该标记。

<{_DSML_TOKEN}tool_calls>
<{_DSML_TOKEN}invoke name="read_file">
<{_DSML_TOKEN}parameter name="path" string="true">文件路径</{_DSML_TOKEN}parameter>
</{_DSML_TOKEN}invoke>
</{_DSML_TOKEN}tool_calls>

可用工具：
"""


@dataclass
class Piece:
    reasoning: str = ""
    content: str = ""
    usage: int | None = None


@dataclass
class _Session:
    session_id: str = ""
    parent_id: int | str | None = None
    acked: int = 0
    digest: str = ""


_state = _Session()


class _MessageStillWipError(RuntimeError):
    """DeepSeek 仍把当前网页会话标记为生成中。"""


def reset_session() -> None:
    """丢掉服务端会话。下一轮会新建，并重发当前本地历史。"""
    global _state
    _state = _Session()


class ContentFilter:
    """流式藏住自定义或 DSML 工具块，避免把调用协议画进回复。"""

    STANDARD_OPEN = "<tool_call>"
    STANDARD_CLOSE = "</tool_call>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = ""
        self.seen_tool = False

    def feed(self, text: str) -> str:
        self.buf += text
        out: list[str] = []
        while self.buf:
            if self.inside == "standard":
                idx = self.buf.find(self.STANDARD_CLOSE)
                if idx < 0:
                    keep = _partial_suffix(self.buf, self.STANDARD_CLOSE)
                    self.buf = self.buf[-keep:] if keep else ""
                    break
                self.buf = self.buf[idx + len(self.STANDARD_CLOSE) :]
                self.inside = ""
                continue
            if self.inside == "dsml":
                close = _DSML_CLOSE_RE.search(self.buf)
                if close is None:
                    # 正文无需保留，只留下可能跨 chunk 的未闭合标签头。
                    tag = self.buf.rfind("<")
                    self.buf = self.buf[tag:] if tag >= 0 and ">" not in self.buf[tag:] else ""
                    break
                self.buf = self.buf[close.end() :]
                self.inside = ""
                continue

            standard_idx = self.buf.find(self.STANDARD_OPEN)
            dsml = _DSML_OPEN_RE.search(self.buf)
            dsml_idx = dsml.start() if dsml is not None else -1
            indexes = [idx for idx in (standard_idx, dsml_idx) if idx >= 0]
            if not indexes:
                keep = _partial_suffix(self.buf, self.STANDARD_OPEN)
                for prefix in ("<|", "<｜"):
                    start = self.buf.rfind(prefix)
                    if start >= 0 and ">" not in self.buf[start:]:
                        keep = max(keep, len(self.buf) - start)
                emit = self.buf[:-keep] if keep else self.buf
                self.buf = self.buf[-keep:] if keep else ""
                if emit:
                    out.append(emit)
                break
            idx = min(indexes)
            if idx:
                out.append(self.buf[:idx])
            if idx == standard_idx:
                self.buf = self.buf[idx + len(self.STANDARD_OPEN) :]
                self.inside = "standard"
            else:
                self.buf = self.buf[dsml.end() :] if dsml is not None else ""
                self.inside = "dsml"
            self.seen_tool = True
        return "".join(out)

    def flush(self) -> str:
        """生成结束时吐出未进入工具块的普通文本尾部。"""
        if self.inside:
            self.buf = ""
            return ""
        text, self.buf = self.buf, ""
        return text


class Assembler:
    """把 SSE 补丁收成思考文本和回复文本。

    只有 v 的包延续上一次 APPEND。v 若是带 type 的片段对象，则追加到 fragments，
    之后的纯文本续写新片段的 content（抓包里思考结束、正式回复开始就是这种包）。
    """

    def __init__(self) -> None:
        self.root: dict = {}
        self.cursor: list[str] | None = None
        self.response_message_id: int | str | None = None
        self.usage: int | None = None
        self._think_n = 0
        self._answer_n = 0

    def feed(self, obj: dict) -> tuple[str, str]:
        if not isinstance(obj, dict):
            return "", ""
        if "v" not in obj and "p" not in obj and "o" not in obj:
            if obj.get("response_message_id") is not None:
                self.response_message_id = obj["response_message_id"]
            return "", ""
        self._apply(obj)
        return self._take_delta()

    def _apply(self, obj: dict) -> None:
        if str(obj.get("o") or "").upper() == "BATCH":
            base = _parts(obj.get("p") or "")
            for item in obj.get("v") or []:
                if not isinstance(item, dict):
                    continue
                sub = _parts(item.get("p") or "")
                self._apply({
                    "p": "/".join(base + sub),
                    "o": item.get("o") or "SET",
                    "v": item.get("v"),
                })
            return

        if "p" not in obj and "o" not in obj and "v" in obj:
            val = obj["v"]
            if isinstance(val, str) and self.cursor:
                self._apply({"p": "/".join(self.cursor), "o": "APPEND", "v": val})
                return
            if isinstance(val, dict):
                self._merge_root(val)
            return

        path = _parts(obj.get("p") or "")
        val = obj.get("v")
        op = str(obj.get("o") or "").upper()
        if not op:
            if isinstance(val, str) and path and path[-1] in {"content", "thinking_content"}:
                op = "APPEND"
            else:
                op = "SET"

        if op == "APPEND" and _is_fragment_payload(val):
            items = val if isinstance(val, list) else [val]
            self._push_fragments(items)
            return
        if op == "APPEND" and isinstance(val, str):
            self.cursor = path
            cur = self._get(path)
            self._set(path, (cur if isinstance(cur, str) else "") + val)
            return
        if op == "APPEND" and isinstance(val, list):
            cur = self._get(path)
            if isinstance(cur, list):
                cur.extend(val)
                if path[-1:] == ["fragments"]:
                    self.cursor = ["response", "fragments", "-1", "content"]
                return
        if op == "APPEND" and isinstance(val, dict) and path[-1:] == ["fragments"]:
            cur = self._get(path)
            if isinstance(cur, list):
                cur.append(val)
                self.cursor = ["response", "fragments", "-1", "content"]
                return
        self._set(path, val)

    def _merge_root(self, val: dict) -> None:
        if not isinstance(self.root, dict):
            self.root = {}
        for key, item in val.items():
            if key == "response" and isinstance(item, dict) and isinstance(self.root.get("response"), dict):
                self.root["response"].update(item)
            else:
                self.root[key] = item

    def _push_fragments(self, frags: list) -> None:
        resp = self.root.get("response")
        if not isinstance(resp, dict):
            resp = {}
            self.root["response"] = resp
        cur = resp.get("fragments")
        if not isinstance(cur, list):
            cur = []
            resp["fragments"] = cur
        for frag in frags:
            if isinstance(frag, dict):
                cur.append(dict(frag))
        self.cursor = ["response", "fragments", "-1", "content"]

    def _take_delta(self) -> tuple[str, str]:
        think, answer = self._collect()
        reasoning = think[self._think_n :]
        content = answer[self._answer_n :]
        self._think_n = len(think)
        self._answer_n = len(answer)
        resp = self.root.get("response") if isinstance(self.root, dict) else None
        if isinstance(resp, dict):
            if resp.get("message_id") is not None:
                self.response_message_id = resp["message_id"]
            usage = resp.get("accumulated_token_usage")
            if isinstance(usage, (int, float)) and not isinstance(usage, bool):
                self.usage = int(usage)
        return reasoning, content

    def _collect(self) -> tuple[str, str]:
        resp = self.root.get("response") if isinstance(self.root, dict) else None
        frags = resp.get("fragments") if isinstance(resp, dict) else None
        if not isinstance(frags, list):
            return "", ""
        think: list[str] = []
        answer: list[str] = []
        for frag in frags:
            if not isinstance(frag, dict):
                continue
            typ = str(frag.get("type") or "").upper()
            content = frag.get("content") if isinstance(frag.get("content"), str) else ""
            extra = frag.get("thinking_content") if isinstance(frag.get("thinking_content"), str) else ""
            if typ in _THINK_TYPES:
                think.append(extra or content)
            elif typ in _ANSWER_TYPES or (typ == "" and content):
                answer.append(content)
        return "".join(think), "".join(answer)

    def _get(self, path: list[str]):
        cur = self.root
        for key in path:
            if isinstance(cur, list):
                idx = _index(cur, key)
                if idx is None:
                    return None
                cur = cur[idx]
            elif isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        return cur

    def _set(self, path: list[str], val) -> None:
        if not path:
            if isinstance(val, dict):
                self.root = val
            return
        if not isinstance(self.root, dict):
            self.root = {}
        cur = self.root
        for key in path[:-1]:
            if isinstance(cur, list):
                idx = _index(cur, key)
                if idx is None:
                    return
                cur = cur[idx]
            elif isinstance(cur, dict):
                nxt = cur.get(key)
                if nxt is None:
                    nxt = {}
                    cur[key] = nxt
                cur = nxt
            else:
                return
        last = path[-1]
        if isinstance(cur, list):
            idx = _index(cur, last)
            if idx is None:
                return
            cur[idx] = val
        elif isinstance(cur, dict):
            cur[last] = val


def parse_tool_calls(text: str) -> tuple[str, list[dict]]:
    """抽出自定义 XML 或 DeepSeek DSML，统一成 OpenAI tool_calls。"""
    calls: list[dict] = []
    visible: list[str] = []
    last = 0
    for match in _CALL_BLOCK_RE.finditer(text):
        visible.append(text[last : match.start()])
        if match.group("standard") is not None:
            parsed = _parse_standard_call(match.group("standard"))
        else:
            parsed = _parse_dsml_calls(match.group("dsml") or "")
        if parsed is None:
            visible.append(match.group(0))
        else:
            for name, args in parsed:
                calls.append(_openai_tool_call(name, args, len(calls) + 1))
        last = match.end()
    visible.append(text[last:])
    return "".join(visible).strip(), calls


def has_tool_call_markup(text: str) -> bool:
    """回复中是否还残留未解析的工具协议标记。"""
    return bool(
        re.search(r"<\s*/?\s*tool_call\b", text, re.IGNORECASE)
        or _DSML_ANY_TAG_RE.search(text)
    )


def _parse_standard_call(raw: str) -> list[tuple[str, object]] | None:
    obj = _load_obj(raw)
    if not isinstance(obj, dict):
        return None
    name = str(obj.get("name") or obj.get("tool") or "").strip()
    if not name and isinstance(obj.get("function"), dict):
        name = str(obj["function"].get("name") or "").strip()
        args = obj["function"].get("arguments") or {}
    else:
        args = obj.get("arguments", obj.get("parameters", {}))
    return [(name, args)] if name else None


def _parse_dsml_calls(block: str) -> list[tuple[str, object]] | None:
    parsed: list[tuple[str, object]] = []
    invokes = list(_DSML_INVOKE_RE.finditer(block))
    if not invokes:
        return None
    inner = re.sub(
        rf"\A\s*<{_DSML_MARKER_RE}{_DSML_CALL_BLOCK_NAME_RE}\s*>",
        "",
        block,
        count=1,
        flags=re.IGNORECASE,
    )
    inner = re.sub(
        rf"</{_DSML_MARKER_RE}{_DSML_CALL_BLOCK_NAME_RE}\s*>\s*\Z",
        "",
        inner,
        count=1,
        flags=re.IGNORECASE,
    )
    if _DSML_INVOKE_RE.sub("", inner).strip():
        return None
    for invoke in invokes:
        name = _xml_attr(invoke.group("attrs"), "name")
        if not name:
            return None
        body = invoke.group("body")
        params = list(_DSML_PARAMETER_RE.finditer(body))
        args: dict[str, object]
        if params:
            # 参数标签之外只能有空白；否则说明模型生成了残缺 DSML。
            residue = _DSML_PARAMETER_RE.sub("", body)
            if residue.strip():
                return None
            args = {}
            for param in params:
                key = _xml_attr(param.group("attrs"), "name")
                if not key or key in args:
                    return None
                raw_value = param.group("value")
                string_flag = _xml_attr(param.group("attrs"), "string").lower()
                if string_flag == "true":
                    value: object = raw_value
                else:
                    try:
                        value = json.loads(raw_value.strip())
                    except json.JSONDecodeError:
                        if string_flag == "false":
                            return None
                        value = raw_value
                args[key] = value
        else:
            raw_args = body.strip()
            if not raw_args:
                args = {}
            else:
                obj = _load_obj(raw_args)
                if obj is None:
                    return None
                args = obj
        parsed.append((name, args))
    return parsed


def _xml_attr(attrs: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1",
        attrs,
        re.DOTALL | re.IGNORECASE,
    )
    return match.group(2) if match else ""


def _openai_tool_call(name: str, args: object, index: int) -> dict:
    if not isinstance(args, str):
        args = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def stream_chat(messages: list[dict]):
    """流式问一次；WIP 时换新会话、重发完整历史并限次退避。"""
    prompt, cont = _plan_prompt(messages)
    if not cont:
        reset_session()
    delays = (1.0, 2.0, 4.0)
    for attempt in range(len(delays) + 1):
        emitted = False
        stream = _stream_chat_request(messages, prompt, cont)
        try:
            for piece in stream:
                emitted = True
                yield piece
            return
        except _MessageStillWipError:
            reset_session()
            if emitted or attempt >= len(delays):
                raise RuntimeError(
                    "DeepSeek 会话持续处于生成中；已自动换会话重试，仍未恢复，请稍后再试"
                ) from None
            time.sleep(delays[attempt])
            prompt = format_transcript(messages, tools=True)
            cont = False
        finally:
            stream.close()


def _stream_chat_request(messages: list[dict], prompt: str, cont: bool):
    """执行一次网页流请求；会话恢复策略由 stream_chat 统一处理。"""
    resp = None
    response_id = None
    try:
        sid = _ensure_session()
        parent = _state.parent_id if cont else None
        resp = _open_completion(
            sid,
            parent,
            prompt,
            thinking=config.THINKING,
            search=config.DEEPSEEK_SEARCH,
        )
        assembler = Assembler()
        for event, data in _iter_sse(resp):
            if event == "close" or data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if _is_message_still_wip_obj(obj):
                raise _MessageStillWipError("DeepSeek message still wip")
            if obj.get("code") not in (None, 0) and "v" not in obj and "p" not in obj:
                raise RuntimeError(f"接口返回错误：{_err_text(obj)}")
            reasoning, content = assembler.feed(obj)
            if assembler.response_message_id is not None:
                response_id = assembler.response_message_id
            if reasoning or content or assembler.usage is not None:
                yield Piece(reasoning=reasoning, content=content, usage=assembler.usage)
    except GeneratorExit:
        if response_id is not None:
            _commit(messages, response_id)
        raise
    else:
        _commit(messages, response_id)
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def complete_plain(messages: list[dict]) -> str:
    """压缩摘要用的一次性请求，不占用对话会话，也不带工具说明。"""
    prompt = format_transcript(messages, tools=False)
    created = _request_json("POST", "/api/v0/chat_session/create", {"character_id": None})
    sid = _session_id(created)
    resp = _open_completion(sid, None, prompt, thinking=False, search=False)
    assembler = Assembler()
    try:
        for event, data in _iter_sse(resp):
            if event == "close" or data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                if obj.get("code") not in (None, 0) and "v" not in obj and "p" not in obj:
                    raise RuntimeError(f"接口返回错误：{_err_text(obj)}")
                assembler.feed(obj)
    finally:
        resp.close()
    _think, answer = assembler._collect()
    return answer.strip()


def format_transcript(
    messages: list[dict],
    *,
    tools: bool,
    names: dict[str, str] | None = None,
) -> str:
    names = _tool_names(messages) if names is None else names
    blocks: list[str] = []
    guide_done = False
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            text = str(msg.get("content") or "").strip()
            if text:
                blocks.append("【系统】\n" + text)
            if tools and not guide_done:
                blocks.append(_TOOL_GUIDE + _tool_catalog())
                guide_done = True
            continue
        if role == "user":
            blocks.append("【用户】\n" + str(msg.get("content") or ""))
            continue
        if role == "assistant":
            text = _format_assistant(msg)
            if text:
                blocks.append("【助手】\n" + text)
            continue
        if role == "tool":
            tid = str(msg.get("tool_call_id") or "")
            name = names.get(tid) or ""
            title = f"【工具结果 {name}】" if name else "【工具结果】"
            blocks.append(title + "\n" + str(msg.get("content") or ""))
    if tools and not guide_done:
        blocks.insert(0, _TOOL_GUIDE + _tool_catalog())
    return "\n\n".join(blocks).strip()


def _plan_prompt(messages: list[dict]) -> tuple[str, bool]:
    if (
        _state.session_id
        and _state.acked > 0
        and _state.acked <= len(messages)
        and _digest(messages[: _state.acked]) == _state.digest
    ):
        pending = list(messages[_state.acked :])
        while pending and pending[0].get("role") == "assistant":
            pending = pending[1:]
        if pending:
            return format_transcript(pending, tools=False, names=_tool_names(messages)), True
    return format_transcript(messages, tools=True), False


def _commit(messages: list[dict], response_id) -> None:
    if response_id is None:
        # 没有消息 id 就接不上 parent_message_id，下一轮重发全文
        reset_session()
        return
    _state.parent_id = _as_id(response_id)
    _state.acked = len(messages)
    _state.digest = _digest(messages[: _state.acked])


def _digest(messages: list[dict]) -> str:
    payload = []
    for msg in messages:
        payload.append({
            "role": msg.get("role"),
            "content": msg.get("content"),
            "tool_calls": msg.get("tool_calls"),
            "tool_call_id": msg.get("tool_call_id"),
        })
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _tool_names(messages: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            tid = tc.get("id")
            name = ((tc.get("function") or {}).get("name")) or ""
            if tid and name:
                names[str(tid)] = str(name)
    return names


def _format_assistant(msg: dict) -> str:
    content = str(msg.get("content") or "").strip()
    blocks = [content] if content else []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {}
        blocks.append(
            "<tool_call>\n"
            + json.dumps({"name": fn.get("name"), "arguments": args}, ensure_ascii=False)
            + "\n</tool_call>"
        )
    return "\n".join(blocks).strip()


def _tool_catalog() -> str:
    lines = []
    for tool in get_tools():
        fn = tool.get("function") or {}
        lines.append(json.dumps({
            "name": fn.get("name"),
            "description": fn.get("description"),
            "parameters": fn.get("parameters"),
        }, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def _load_obj(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _ensure_session() -> str:
    if _state.session_id:
        return _state.session_id
    data = _request_json("POST", "/api/v0/chat_session/create", {"character_id": None})
    _state.session_id = _session_id(data)
    _state.parent_id = None
    return _state.session_id


def _session_id(data: dict) -> str:
    if not isinstance(data, dict):
        raise RuntimeError("创建会话的返回不是 JSON 对象")
    if data.get("chat_session_id"):
        return str(data["chat_session_id"])
    biz = data.get("data")
    if isinstance(biz, dict):
        biz = biz.get("biz_data") or biz
    if isinstance(biz, dict):
        chat = biz.get("chat_session") if isinstance(biz.get("chat_session"), dict) else biz
        if isinstance(chat, dict) and chat.get("id"):
            return str(chat["id"])
        if biz.get("chat_session_id"):
            return str(biz["chat_session_id"])
    raise RuntimeError(
        "创建会话成功但没有 chat_session_id：" + json.dumps(data, ensure_ascii=False)[:400]
    )


def _open_completion(sid: str, parent, prompt: str, *, thinking: bool, search: bool):
    body = {
        "chat_session_id": sid,
        "parent_message_id": parent,
        "model_type": None,
        "prompt": prompt or "继续",
        "ref_file_ids": [],
        "thinking_enabled": bool(thinking),
        "search_enabled": bool(search),
        "action": None,
        "preempt": False,
    }
    headers = _headers("text/event-stream")
    headers["x-ds-pow-response"] = _pow_response("/api/v0/chat/completion")
    headers["Referer"] = f"{config.DEEPSEEK_BASE}/a/chat/s/{sid}"
    req = urllib.request.Request(
        config.DEEPSEEK_BASE + "/api/v0/chat/completion",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError:
        raise
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" in ctype and "text/event-stream" not in ctype:
        raw = resp.read().decode("utf-8", errors="replace")
        resp.close()
        if _is_message_still_wip(raw):
            raise _MessageStillWipError("DeepSeek message still wip")
        raise RuntimeError(f"接口没有返回事件流：{raw[:600]}")
    return resp


def _is_message_still_wip(raw: str) -> bool:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return "message still wip" in raw.lower()
    return _is_message_still_wip_obj(obj)


def _is_message_still_wip_obj(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    data = obj.get("data")
    if not isinstance(data, dict):
        return False
    candidates = [data]
    nested = data.get("biz_data")
    if isinstance(nested, dict):
        candidates.append(nested)
    return any(
        biz.get("biz_code") == 11
        or "message still wip" in str(biz.get("biz_msg") or "").lower()
        for biz in candidates
    )


def _request_json(method: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        config.DEEPSEEK_BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method=method,
        headers=_headers("application/json"),
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, dict) and data.get("code") not in (None, 0):
        raise RuntimeError(f"接口返回错误：{_err_text(data)}")
    if not isinstance(data, dict):
        raise RuntimeError("接口返回不是 JSON 对象")
    return data


def _pow_response(target_path: str) -> str:
    """向 create_pow_challenge 要一道新题，解出后放进 x-ds-pow-response。题目大约 5 分钟过期，不能复用抓包。"""
    data = _request_json("POST", "/api/v0/chat/create_pow_challenge", {"target_path": target_path})
    biz = ((data.get("data") or {}).get("biz_data") or {})
    challenge = biz.get("challenge") if isinstance(biz, dict) else None
    if not isinstance(challenge, dict):
        raise RuntimeError("没有拿到 PoW 题目：" + json.dumps(data, ensure_ascii=False)[:400])
    return _solve_pow(challenge)


_WASM_PATH = Path(__file__).resolve().parent / "sha3_wasm_v1.wasm"
_pow_solver = None


class _PowSolver:
    """用 DeepSeek 网页同款 wasm 解 DeepSeekHashV1。当前环境是 Python 3.10，不走 deepseek-pow。"""

    def __init__(self, wasm_path: Path) -> None:
        try:
            import wasmtime
        except ImportError as e:
            raise RuntimeError("缺少 wasmtime，请先执行 pip install wasmtime") from e
        if not wasm_path.is_file():
            raise RuntimeError(f"找不到 PoW 求解文件：{wasm_path}")
        self._wasmtime = wasmtime
        self._engine = wasmtime.Engine()
        self._store = wasmtime.Store(self._engine)
        module = wasmtime.Module.from_file(self._engine, str(wasm_path))
        instance = wasmtime.Instance(self._store, module, [])
        exports = instance.exports(self._store)
        self._memory = exports["memory"]
        self._wasm_solve = exports["wasm_solve"]
        self._add_to_stack_pointer = exports["__wbindgen_add_to_stack_pointer"]
        self._alloc = exports["__wbindgen_export_0"]

    def answer(self, challenge: str, salt: str, difficulty: float, expire_at: int) -> int:
        prefix = f"{salt}_{expire_at}_"
        challenge_ptr, challenge_len = self._write(challenge)
        prefix_ptr, prefix_len = self._write(prefix)
        stack = int(self._add_to_stack_pointer(self._store, -16)) & 0xFFFFFFFF
        try:
            self._wasm_solve(
                self._store,
                stack,
                challenge_ptr,
                challenge_len,
                prefix_ptr,
                prefix_len,
                float(difficulty),
            )
            status = struct.unpack("<i", bytes(self._memory.read(self._store, stack, stack + 4)))[0]
            if status == 0:
                raise RuntimeError("PoW 没有算出答案")
            value = struct.unpack("<d", bytes(self._memory.read(self._store, stack + 8, stack + 16)))[0]
        finally:
            self._add_to_stack_pointer(self._store, 16)
        if not math.isfinite(value):
            raise RuntimeError("PoW 答案无效")
        return int(value) if value == int(value) else value

    def _write(self, text: str) -> tuple[int, int]:
        data = text.encode("utf-8")
        pointer = int(self._alloc(self._store, len(data), 1)) & 0xFFFFFFFF
        if data:
            self._memory.write(self._store, data, pointer)
        return pointer, len(data)


def _solver() -> _PowSolver:
    global _pow_solver
    if _pow_solver is None:
        _pow_solver = _PowSolver(_WASM_PATH)
    return _pow_solver


def _solve_pow(challenge: dict) -> str:
    algo = str(challenge.get("algorithm") or "")
    if algo != "DeepSeekHashV1":
        raise RuntimeError(f"不认识的 PoW 算法：{algo}")
    answer = _solver().answer(
        str(challenge["challenge"]),
        str(challenge["salt"]),
        float(challenge["difficulty"]),
        int(challenge["expire_at"]),
    )
    payload = {
        "algorithm": algo,
        "challenge": challenge["challenge"],
        "salt": challenge["salt"],
        "answer": answer,
        "signature": challenge["signature"],
        "target_path": challenge.get("target_path") or "/api/v0/chat/completion",
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")


def _headers(accept: str) -> dict[str, str]:
    origin = config.DEEPSEEK_BASE
    off = time.altzone if time.localtime().tm_isdst else time.timezone
    headers = {
        "Content-Type": "application/json",
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": origin,
        "Referer": origin + "/",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-platform": "web",
        "x-client-version": "2.5.0",
        "x-client-locale": "zh_CN",
        "x-client-timezone-offset": str(-off),
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
    }
    if config.DEEPSEEK_DEVICE_ID:
        headers["x-device-id"] = config.DEEPSEEK_DEVICE_ID
    if config.DEEPSEEK_COOKIE:
        headers["Cookie"] = config.DEEPSEEK_COOKIE
    if config.DEEPSEEK_API_KEY:
        headers["Authorization"] = f"Bearer {config.DEEPSEEK_API_KEY}"
    return headers


def _iter_sse(resp):
    event = "message"
    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip() or "message"
            continue
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                yield event, payload
            continue
        if line[:1] in "{[":
            yield event, line


def _err_text(obj: dict) -> str:
    return str(obj.get("msg") or obj.get("message") or obj)[:600]


def _as_id(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def _parts(path: str) -> list[str]:
    return [part for part in str(path).split("/") if part]


def _index(seq: list, key: str) -> int | None:
    if key == "-1":
        return len(seq) - 1 if seq else None
    try:
        idx = int(key)
    except (TypeError, ValueError):
        return None
    if idx < 0:
        idx = len(seq) + idx
    if 0 <= idx < len(seq):
        return idx
    return None


def _is_fragment_payload(val) -> bool:
    items = val if isinstance(val, list) else [val]
    if not isinstance(val, (list, dict)) or not items:
        return False
    return all(isinstance(item, dict) and "type" in item for item in items)


def _partial_suffix(buf: str, token: str) -> int:
    limit = min(len(buf), len(token) - 1)
    for size in range(limit, 0, -1):
        if token.startswith(buf[-size:]):
            return size
    return 0
