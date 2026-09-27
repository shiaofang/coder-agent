"""Agent 循环：多轮「思考 → 用工具 → 再思考」，直到最终回答。

还包含防循环逻辑：相同编辑重复、同一 build 错误多次、连续瞎 grep 等。
第一次 Ctrl+C 会取消本轮任务并回到输入提示（不退出程序）。
"""

from __future__ import annotations

import ast
import json
import re
import time

from agent import config, context
from agent.config import (
    MAX_EMPTY_RESPONSE_RETRIES,
    MAX_REASONING_ABORTS,
    MAX_TOOL_ROUNDS,
)
from agent.model import chat_once
from agent.render import console, error, show_tool_call, show_tool_result, show_turn_stats, warn
from agent.terminal import TOOL_SKIPPED_MESSAGE, ask_tool_approval, flush_input_buffer
from agent.tools import execute_tool, preflight_run_command

_EDIT_TOOLS = {"edit_file", "edit_lines"}
_WRITE_TOOLS = _EDIT_TOOLS | {"write_file"}


def extract_error_fingerprint(text: str) -> str:
    """从命令输出里抽出简短错误指纹，用于判断是否反复同一报错。"""
    patterns = [
        r"ERROR:\s*[^\n]+",
        r"error during build:[^\n]*",
        r"SyntaxError:[^\n]+",
        r"TypeError:[^\n]+",
        r"Cannot read properties of undefined \(reading '[^']+'\)",
        r"Module not found:[^\n]*",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(0))[:160]
    for line in text.splitlines():
        if re.search(r"error|fail|exception", line, re.I):
            return line.strip()[:160]
    return ""


def tool_call_signature(name: str, args: dict) -> str:
    """把一次工具调用压成字符串签名，用来检测「完全相同的重复操作」。"""
    if name == "edit_file":
        return "edit|" + json.dumps(
            {k: args.get(k) for k in ("path", "old_text", "new_text", "edits")}, ensure_ascii=False, sort_keys=True
        )[:600]
    if name == "edit_lines":
        return (
            f"lines|{args.get('path')}|{args.get('mode')}|{args.get('start_line')}-"
            f"{args.get('end_line')}|{args.get('content')}"
        )
    if name == "run_command":
        cmd = str(args.get("command") or "")
        if "build" in cmd or "test" in cmd or "lint" in cmd:
            return f"check|{cmd}"
    if name == "web_search":
        return f"search|{args.get('query')}"
    return f"{name}|{json.dumps(args, ensure_ascii=False, sort_keys=True)[:200]}"


# ------------------------------------------------------------------------
#  工具参数 JSON 容错解析
# ------------------------------------------------------------------------

def parse_tool_args(raw: str) -> tuple[dict, str]:
    """尽力把模型给的 arguments 解析成 dict。返回 (args, error)；error 非空表示失败。"""
    text = (raw or "").strip()
    if not text:
        return {}, ""
    try:
        obj = json.loads(text)
        return (obj if isinstance(obj, dict) else {}), ""
    except json.JSONDecodeError as first_err:
        err = f"{first_err.msg} at pos {first_err.pos}"

    candidates: list[str] = []
    t = text
    # 1) 去掉 ```json 围栏
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    candidates.append(t)
    # 2) 去掉尾逗号
    t2 = re.sub(r",\s*([}\]])", r"\1", t)
    candidates.append(t2)
    # 3) 补齐未闭合的括号/引号
    t3 = t2
    if t3.count('"') % 2 == 1:
        t3 += '"'
    depth_obj = t3.count("{") - t3.count("}")
    depth_arr = t3.count("[") - t3.count("]")
    t3 += "]" * max(0, depth_arr) + "}" * max(0, depth_obj)
    candidates.append(t3)
    # 4) 多个 JSON 对象粘在一起（{...}{...}）：只取第一个
    m = re.match(r"^\s*(\{.*?\})\s*\{", t, flags=re.S)
    if m:
        candidates.append(m.group(1))

    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj, ""
        except json.JSONDecodeError:
            continue
    # 5) Python 字面量（单引号 / True / None）
    for cand in candidates:
        try:
            obj = ast.literal_eval(cand)
            if isinstance(obj, dict):
                return obj, ""
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
    return {}, err


def run_agent_turn(messages: list[dict]) -> None:
    """
    Agent 主循环：反复「问模型 → 执行工具 → 把结果喂回模型」，直到模型给出最终回答。

    还包含防循环逻辑：相同编辑重复、同一 build 错误多次、连续瞎 grep 等。
    第一次 Ctrl+C 会取消本轮任务并回到输入提示（不退出程序）。
    """
    # 每条新用户消息开始时，重置「本轮自动」；全局 /auto 不受影响
    config.AUTO_APPROVE = False

    recent_sigs: list[str] = []
    build_error_hist: list[str] = []
    last_error_fp = ""  # 当前正卡住的错误指纹；换了新错误就重置下面的搜索标记
    searched_this_error = False
    reasoning_abort_count = 0
    empty_response_retry_count = 0
    research_call_count = 0  # 连续 web_search/fetch_url 次数，中间没有真正去改代码
    grep_streak = 0  # 连续 grep_search 次数，用于识别"逐个属性瞎猜"
    failed_path_counts: dict[str, int] = {}  # 同一路径反复不存在时，明确阻止把类型标记当路径
    # 本轮开始时的消息长度：中断时丢掉未完成的 assistant/tool 片段，保留用户消息
    start_len = len(messages)

    t0 = time.time()
    tool_count = 0
    last_stats: dict = {}
    total_predicted = 0

    def finish_stats() -> None:
        stats = dict(last_stats)
        if total_predicted:
            stats["predicted_n"] = total_predicted
        used, total = context.usage(messages)
        show_turn_stats(time.time() - t0, stats, tool_count, used, total)
        console.print()

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            context.check(messages)
            res = chat_once(messages)
            if res.stats:
                last_stats = res.stats
                total_predicted += int(res.stats.get("predicted_n") or 0)

            def calibrate() -> None:
                # assistant 消息 append 之后调用，让锚点覆盖到它
                context.calibrate(
                    res.stats.get("prompt_tokens") or res.stats.get("prompt_n"),
                    res.stats.get("predicted_n"),
                    messages,
                )

            if res.looped:
                empty_response_retry_count = 0
                reasoning_abort_count += 1
                reason = "思考超出长度上限" if res.loop_reason == "length" else "思考末尾连续重复"
                if reasoning_abort_count >= MAX_REASONING_ABORTS:
                    error(
                        f"模型连续 {reasoning_abort_count} 次被中断（本次：{reason}），已停止本轮。"
                        "[dim] 可尝试换个说法、拆小任务、/think off，或换更大的模型。[/]"
                    )
                    finish_stats()
                    return
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[系统提示] 你刚才因{reason}而被中断。"
                            "禁止继续长篇分析或重复相同句子，直接给出下一步工具调用；"
                            "如果信息已经够用，直接给出简短结论。"
                        ),
                    }
                )
                continue

            # 只统计连续中断；一次正常回复或工具调用代表模型已经恢复。
            reasoning_abort_count = 0
            content, tool_calls, reasoning = res.content, res.tool_calls, res.reasoning

            if tool_calls:
                empty_response_retry_count = 0
                assistant_msg: dict = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
                if reasoning:
                    assistant_msg["reasoning_content"] = reasoning
                messages.append(assistant_msg)
                calibrate()

                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw_args = tc["function"].get("arguments") or "{}"
                    args, parse_err = parse_tool_args(raw_args)
                    tool_count += 1
                    if parse_err:
                        result = (
                            f"ERROR: arguments 不是合法 JSON（{parse_err}）。"
                            f"请重新调用 {name}，参数必须是一个 JSON 对象，字符串用双引号、不要尾逗号。"
                            f"\n原始内容前 200 字：{raw_args[:200]}"
                        )
                        show_tool_call(name, args)
                        show_tool_result(result, name)
                    else:
                        sig = tool_call_signature(name, args)
                        # Block identical mutating edits looping
                        if name in _EDIT_TOOLS and recent_sigs.count(sig) >= 1:
                            result = (
                                "ERROR: 重复操作 — 完全相同的编辑已经执行过一次，禁止再原样重试。"
                                "先用 read_file 确认文件当前内容（很可能已经生效，或者 old_text/行号已经不对了），"
                                "再决定下一步；如果是同一个报错反复修不好，才需要 web_search 报错原文换思路。"
                            )
                            show_tool_call(name, args)
                            show_tool_result(result, name)
                        elif name == "run_command" and recent_sigs and recent_sigs[-1] == sig:
                            result = (
                                "SKIPPED: 与上一次完全相同的命令已经执行，期间没有其它工具改变状态。"
                                "禁止重复运行；直接解释上一次结果或继续下一步。"
                                "如果输出是 TCP TIME_WAIT，它表示连接已关闭，不是服务仍在运行。"
                            )
                            show_tool_call(name, args)
                            show_tool_result(result, name)
                        else:
                            show_tool_call(name, args)
                            preflight = ""
                            if name == "run_command":
                                preflight = preflight_run_command(
                                    str(args.get("command") or ""),
                                    str(args["cwd"]) if args.get("cwd") else None,
                                )
                            if preflight:
                                result = preflight
                                show_tool_result(result, name)
                            else:
                                approved, reason = ask_tool_approval(name, args)
                                if not approved:
                                    if reason == TOOL_SKIPPED_MESSAGE:
                                        result = TOOL_SKIPPED_MESSAGE
                                    else:
                                        result = "ERROR: user denied tool execution"
                                        if reason:
                                            result += f". 用户说明：{reason}"
                                    show_tool_result(result, name)
                                else:
                                    result = execute_tool(name, args)
                                    path_arg = args.get("path") or args.get("root")
                                    if path_arg:
                                        path_key = str(path_arg).strip().lower()
                                        if result.startswith(("ERROR", "FAIL")):
                                            failed_path_counts[path_key] = failed_path_counts.get(path_key, 0) + 1
                                            if failed_path_counts[path_key] >= 2:
                                                result += (
                                                    "\n\nINVALID_PATH_LOOP: 这个路径已经连续失败。禁止再次使用或搜索同名路径。"
                                                    "如果它来自 list_dir，请注意 [FILE]/[DIR] 只是类型标记，"
                                                    "必须复制标记后面的完整路径；先使用最近一次 list_dir 返回的真实路径。"
                                                )
                                        else:
                                            failed_path_counts.pop(path_key, None)
                                    show_tool_result(result, name)
                            recent_sigs.append(sig)
                            if len(recent_sigs) > 24:
                                recent_sigs = recent_sigs[-24:]

                        # Track repeated command failures → force web search hint
                        if name == "run_command" and (
                            re.search(r"(?m)^exit=[1-9]", result)
                            or result.startswith("FAIL")
                            or result.startswith("ERROR")
                        ):
                            fp = extract_error_fingerprint(result)
                            if fp:
                                build_error_hist.append(fp)
                                if fp != last_error_fp:
                                    last_error_fp = fp
                                    searched_this_error = False
                                same = sum(1 for x in build_error_hist if x == fp)
                                if same >= 2 and not searched_this_error:
                                    if config.TAVILY_API_KEY:
                                        hint = (
                                            f"\n\nLOOP_HINT: 同一报错已经出现 {same} 次：\n  {fp}\n"
                                            "下一步必须：web_search（查询词=这条报错原文 + 项目实际用的框架/库名），"
                                            "再 fetch_url 打开一个相关结果；禁止重复刚才的改法。"
                                        )
                                    else:
                                        hint = (
                                            f"\n\nLOOP_HINT: 同一报错已经出现 {same} 次：\n  {fp}\n"
                                            "禁止重复刚才的改法；重新读报错指向的文件与相关调用处，换一种思路修。"
                                        )
                                    result = result + hint
                                    searched_this_error = True
                                if same >= 3:
                                    result = result + (
                                        "\n\nESCALATE: 本地小修已经反复失败。"
                                        "请基于已经掌握的信息，用项目实际框架/库的已知正确写法"
                                        "重写这个文件/组件里出问题的部分，禁止再对同几行做微调。"
                                    )

                        if name == "web_search":
                            searched_this_error = True

                        # 搜索/抓取次数堆积但一直没落地改代码 → 强制收敛
                        if name in {"web_search", "fetch_url"}:
                            research_call_count += 1
                            if research_call_count >= 3:
                                result = result + (
                                    f"\n\nSTOP_SEARCHING: 已经调用了 {research_call_count} 次 "
                                    "web_search/fetch_url，还没有真正改代码。"
                                    "禁止再搜索，必须直接根据已拿到的信息用 edit_file/edit_lines 做一次具体修改，"
                                    "再用 run_command 重新跑检查/构建看结果。"
                                )
                        elif name in _WRITE_TOOLS:
                            research_call_count = 0

                        # 连续 grep_search（常见于无目的枚举）→ 提醒回到报错本身
                        if name == "grep_search":
                            grep_streak += 1
                            if grep_streak >= 3:
                                result = result + (
                                    f"\n\nSTOP_GUESSING: 已经连续 {grep_streak} 次 grep_search，这是在瞎猜。"
                                    "禁止再无目的枚举关键词；必须回到报错信息里的文件名/行号/标识符，"
                                    "用 read_file 读上下文。"
                                )
                        else:
                            grep_streak = 0

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id") or name,
                            "content": result,
                        }
                    )
                console.print()
                continue

            # final answer（走到这里说明没有 tool_calls）
            if content:
                empty_response_retry_count = 0
                messages.append({"role": "assistant", "content": content})
                calibrate()
            else:
                empty_response_retry_count += 1
                if empty_response_retry_count > MAX_EMPTY_RESPONSE_RETRIES:
                    error(
                        "模型连续只返回思考、没有正文或工具调用，已停止本轮。"
                        "[dim] 请重试，或用 /new 后换一种说法。[/]"
                    )
                    finish_stats()
                    return
                warn(
                    f"模型没有给出最终回复，正在要求其继续并总结"
                    f"（{empty_response_retry_count}/{MAX_EMPTY_RESPONSE_RETRIES}）"
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[系统提示] 你上一条只有思考，没有正文或工具调用。"
                            "如果任务尚未完成，立即输出下一步完整工具调用；"
                            "如果已经完成，立即用简短中文总结改动、涉及文件和验证结果。"
                            "不要继续分析，不要返回空回复。"
                        ),
                    }
                )
                continue
            finish_stats()
            return

        error(f"工具轮次达到上限[dim]（{MAX_TOOL_ROUNDS}），请再发一条消息让模型继续[/]")
        finish_stats()
    except KeyboardInterrupt:
        console.print()
        warn("已取消当前任务（提示符下再按 Ctrl+C 退出）")
        console.print()
        del messages[start_len:]
        config.AUTO_APPROVE = False
        flush_input_buffer()
