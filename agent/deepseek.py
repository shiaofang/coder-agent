"""DeepSeek 网页协议客户端：/api/v0/chat/completion。

请求体是单条 prompt + chat_session_id，返回 SSE 补丁流（APPEND / SET / BATCH），
没有 OpenAI 的 tools 字段。工具调用约定写进 prompt，用 <tool_call> 标记收回，
再还原成 loop.py 认识的 tool_calls。

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

_TOOL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_THINK_TYPES = {"THINK", "THINKING", "THOUGHT"}
_ANSWER_TYPES = {"RESPONSE", "REPLY"}

_TOOL_GUIDE = """需要调用工具时，只输出下面这种标记，不要用 markdown 代码块包住。arguments 必须是 JSON 对象。可以连续输出多个标记。标记以外不要解释你准备调用工具。
不需要工具时直接用中文回答，不要输出该标记。

<tool_call>
{"name":"read_file","arguments":{"path":"文件路径"}}
</tool_call>

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


def reset_session() -> None:
    """丢掉服务端会话。下一轮会新建，并重发当前本地历史。"""
    global _state
    _state = _Session()


class ContentFilter:
    """流式输出时藏住 <tool_call>…</tool_call>，避免把工具 JSON 画进回复。"""

    OPEN = "<tool_call>"
    CLOSE = "</tool_call>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False
        self.seen_tool = False

    def feed(self, text: str) -> str:
        self.buf += text
        out: list[str] = []
        while self.buf:
            if self.inside:
                idx = self.buf.find(self.CLOSE)
                if idx < 0:
                    keep = _partial_suffix(self.buf, self.CLOSE)
                    self.buf = self.buf[-keep:] if keep else ""
                    break
                self.buf = self.buf[idx + len(self.CLOSE) :]
                self.inside = False
                continue
            idx = self.buf.find(self.OPEN)
            if idx < 0:
                keep = _partial_suffix(self.buf, self.OPEN)
                emit = self.buf[:-keep] if keep else self.buf
                self.buf = self.buf[-keep:] if keep else ""
                if emit:
                    out.append(emit)
                break
            if idx:
                out.append(self.buf[:idx])
            self.buf = self.buf[idx + len(self.OPEN) :]
            self.inside = True
            self.seen_tool = True
        return "".join(out)


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
    """从回复里抽出工具调用。解析失败的标记原样留在正文里。"""
    calls: list[dict] = []
    visible: list[str] = []
    last = 0
    for match in _TOOL_RE.finditer(text):
        visible.append(text[last : match.start()])
        obj = _load_obj(match.group(1))
        name = ""
        args: object = {}
        if isinstance(obj, dict):
            name = str(obj.get("name") or obj.get("tool") or "").strip()
            if not name and isinstance(obj.get("function"), dict):
                name = str(obj["function"].get("name") or "").strip()
                args = obj["function"].get("arguments") or {}
            else:
                args = obj.get("arguments", obj.get("parameters", {}))
        if name:
            if not isinstance(args, str):
                args = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
            calls.append({
                "id": f"call_{len(calls) + 1}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            })
        else:
            visible.append(match.group(0))
        last = match.end()
    visible.append(text[last:])
    return "".join(visible).strip(), calls


def stream_chat(messages: list[dict]):
    """流式问一次。正常结束或已经拿到 response_message_id 后，记下会话进度。"""
    prompt, cont = _plan_prompt(messages)
    if not cont:
        reset_session()
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
        raise RuntimeError(f"接口没有返回事件流：{raw[:600]}")
    return resp


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
