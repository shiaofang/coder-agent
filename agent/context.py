"""上下文预算：估算 token 用量、两级压缩（折叠旧工具结果 / 模型总结旧对话）。

本地 GGUF 的上下文通常只有 16k~32k，历史一多就会撞上限报错。
这里在每轮工具循环前检查用量，快满时自动压缩，并给状态栏提供 ctx 百分比。
"""

from __future__ import annotations

import json

from agent import config
from agent.render import console, info, warn
from agent.tools_schema import get_tools

# 估算策略：以服务端最近一次上报的真实 token 数为「锚点」（覆盖 messages[:_anchor_index]），
# 锚点之后新增的消息按 字符数 × _ratio 估算；_ratio 用相邻两次锚点的差值校准。
_ratio = 1 / 3.2  # 默认经验值（中英混合 + JSON）
_last_prompt_tokens = 0
_anchor_tokens = 0
_anchor_index = 0  # 锚点覆盖到的消息条数；0 = 无锚点
_anchor_chars = 0  # 锚点时 messages[:_anchor_index] 的字符数（校准用）

_KEEP_TOOL_ROUNDS = 2  # 最近 N 轮工具结果保持全文
_KEEP_TAIL_MESSAGES = 4  # 模型总结时保留最近多少条消息
_FOLD_MIN_CHARS = 300  # 工具结果超过这个长度才折叠
_MUTATING_TOOLS = {"write_file", "edit_file", "edit_lines", "delete_path", "move_file"}


def _msg_chars(messages: list[dict]) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


def _chars(messages: list[dict]) -> int:
    return len(json.dumps(get_tools(), ensure_ascii=False)) + _msg_chars(messages)


def reset_anchor() -> None:
    """消息列表被整体改动（压缩 / 新会话 / 恢复）后调用，回到纯字符估算。"""
    global _anchor_tokens, _anchor_index, _anchor_chars
    _anchor_tokens = _anchor_index = _anchor_chars = 0


def calibrate(prompt_tokens: int | None, predicted_n: int | None, messages: list[dict]) -> None:
    """一次 chat_once 之后调用（assistant 消息已 append）：
    prompt_tokens 覆盖请求时的全部消息，predicted_n 是刚生成的 assistant 消息。"""
    global _ratio, _last_prompt_tokens, _anchor_tokens, _anchor_index, _anchor_chars
    if not prompt_tokens or prompt_tokens <= 0:
        return
    _last_prompt_tokens = int(prompt_tokens)
    total = int(prompt_tokens) + int(predicted_n or 0)
    chars_now = _chars(messages)

    # 用相邻两个锚点的差值校准 字符→token 比例（主要反映工具结果的密度）
    if _anchor_index and _anchor_index <= len(messages):
        d_tokens = total - _anchor_tokens
        d_chars = chars_now - _anchor_chars
        if d_chars > 400 and d_tokens > 0:
            r = d_tokens / d_chars
            if 0.08 <= r <= 1.5:
                _ratio = 0.5 * _ratio + 0.5 * r
    elif chars_now > 0:
        r = total / chars_now
        if 0.1 <= r <= 1.5:
            _ratio = r

    _anchor_tokens = total
    _anchor_index = len(messages)
    _anchor_chars = chars_now


def estimate(messages: list[dict]) -> int:
    if _anchor_index and _anchor_index <= len(messages):
        extra = _msg_chars(messages[_anchor_index:])
        return int(_anchor_tokens + extra * _ratio)
    return int(_chars(messages) * _ratio)


def usage(messages: list[dict]) -> tuple[int, int]:
    """(已用 token 估算, 总上下文)。"""
    return estimate(messages), config.n_ctx()


def last_prompt_tokens() -> int:
    return _last_prompt_tokens


def status_text(messages: list[dict]) -> str:
    used, total = usage(messages)
    pct = used / total * 100 if total else 0
    return f"ctx {pct:.0f}% {used / 1000:.1f}k/{total / 1000:.0f}k"


# ------------------------------------------------------------------------
#  Level 1：折叠旧工具结果
# ------------------------------------------------------------------------

def _tool_names_by_id(messages: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                out[str(tc.get("id"))] = str((tc.get("function") or {}).get("name") or "")
    return out


def fold_old_tool_results(messages: list[dict], keep_rounds: int = _KEEP_TOOL_ROUNDS) -> int:
    """把倒数 keep_rounds 轮之前的长工具结果替换成一行摘要。返回折叠条数。"""
    # 找到最近 keep_rounds 个带 tool_calls 的 assistant 消息的位置
    rounds = [i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls")]
    if len(rounds) <= keep_rounds:
        return 0
    cutoff = rounds[-keep_rounds]
    names = _tool_names_by_id(messages)
    folded = 0
    for i in range(1, cutoff):
        m = messages[i]
        if m.get("role") != "tool":
            continue
        content = str(m.get("content") or "")
        if len(content) < _FOLD_MIN_CHARS or content.startswith("[已省略"):
            continue
        name = names.get(str(m.get("tool_call_id")), "tool")
        if name in _MUTATING_TOOLS:
            # 写操作的结果本身很短，且对后续决策有用，保留
            continue
        first = content.splitlines()[0][:80] if content.strip() else ""
        n_lines = content.count("\n") + 1
        m["content"] = f"[已省略 {name} 的结果，{n_lines} 行] {first}"
        folded += 1
    if folded:
        reset_anchor()
    return folded


# ------------------------------------------------------------------------
#  Level 2：模型总结
# ------------------------------------------------------------------------

_SUMMARY_PROMPT = (
    "把上面的对话压缩成一份供你自己继续工作的备忘录，用中文、要点式，不超过 400 字。必须包含："
    "1) 用户的目标与约束；2) 已经改了哪些文件、改了什么；3) 已验证/未验证的结果；"
    "4) 当前卡住的问题与下一步。只输出备忘录本身。"
)


def _safe_tail_start(messages: list[dict], keep: int) -> int:
    """找到一个不会把 assistant(tool_calls) / tool 配对拆开的尾部起点。"""
    start = max(1, len(messages) - keep)
    while start > 1 and messages[start].get("role") != "user":
        start -= 1
    return start


def summarize(messages: list[dict]) -> bool:
    """用模型把旧对话总结成一条消息，原地替换。返回是否成功。"""
    from agent.model import chat_plain

    if len(messages) <= _KEEP_TAIL_MESSAGES + 2:
        return False
    tail_start = _safe_tail_start(messages, _KEEP_TAIL_MESSAGES)
    if tail_start <= 1:
        return False
    old = messages[1:tail_start]
    # 总结请求本身不带工具；把 tool 消息转成普通文本，避免服务端校验 tool_call_id
    flat: list[dict] = [{"role": "system", "content": "你是善于总结的编程助手。"}]
    for m in old:
        role = m.get("role")
        if role == "tool":
            flat.append({"role": "user", "content": f"[工具结果]\n{str(m.get('content') or '')[:1500]}"})
        elif role == "assistant":
            text = str(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                text += f"\n[调用 {fn.get('name')}] {str(fn.get('arguments') or '')[:300]}"
            flat.append({"role": "assistant", "content": text.strip() or "(调用工具)"})
        else:
            flat.append({"role": "user", "content": str(m.get("content") or "")[:3000]})
    flat.append({"role": "user", "content": _SUMMARY_PROMPT})

    with console.status("[dim]正在压缩对话历史…[/]", spinner="dots"):
        try:
            summary = chat_plain(flat)
        except Exception as e:
            warn(f"压缩失败：{type(e).__name__}: {e}")
            return False
    if not summary:
        return False
    messages[1:tail_start] = [
        {"role": "user", "content": f"[对话摘要 — 之前的对话已压缩]\n{summary}"},
        {"role": "assistant", "content": "收到，我会基于以上摘要继续。"},
    ]
    reset_anchor()
    return True


# ------------------------------------------------------------------------
#  自动检查
# ------------------------------------------------------------------------

def check(messages: list[dict]) -> None:
    """每轮工具循环前调用：超阈值就压缩。"""
    used, total = usage(messages)
    if not total:
        return
    ratio = used / total
    if ratio >= config.COMPACT_L2_RATIO:
        folded = fold_old_tool_results(messages, keep_rounds=1)
        used2 = estimate(messages)
        if used2 / total >= config.COMPACT_L2_RATIO:
            warn(f"上下文已用 {ratio * 100:.0f}%，自动总结旧对话…")
            if summarize(messages):
                info(f"已压缩：{used / 1000:.1f}k → {estimate(messages) / 1000:.1f}k tokens")
        elif folded:
            info(f"上下文已用 {ratio * 100:.0f}%，折叠了 {folded} 条旧工具结果")
    elif ratio >= config.COMPACT_L1_RATIO:
        folded = fold_old_tool_results(messages)
        if folded:
            info(f"上下文已用 {ratio * 100:.0f}%，折叠了 {folded} 条旧工具结果")


def compact_now(messages: list[dict]) -> None:
    """/compact：先折叠再总结。"""
    before = estimate(messages)
    folded = fold_old_tool_results(messages, keep_rounds=1)
    ok = summarize(messages)
    after = estimate(messages)
    if ok or folded:
        info(f"已压缩：{before / 1000:.1f}k → {after / 1000:.1f}k tokens（折叠 {folded} 条工具结果{'，已总结旧对话' if ok else ''}）")
    else:
        info("对话还很短，无需压缩")
