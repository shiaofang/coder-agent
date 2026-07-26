"""Agent 循环：多轮「思考 → 用工具 → 再思考」，直到最终回答。

还包含防循环逻辑：相同编辑重复、同一 build 错误多次、连续瞎 grep 等。
第一次 Ctrl+C 会取消本轮任务并回到输入提示（不退出程序）。
"""

from __future__ import annotations

import json
import re
import sys

from agent import config
from agent.config import MAX_REASONING_ABORTS, MAX_TOOL_ROUNDS
from agent.model import chat_once
from agent.terminal import C, ask_tool_approval, flush_input_buffer, paint, show_tool_call, show_tool_result
from agent.tools import execute_tool

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
        return f"edit|{args.get('path')}|{args.get('old_text')}|{args.get('new_text')}"
    if name == "multi_edit":
        return "multiedit|" + json.dumps(args.get("edits"), ensure_ascii=False, sort_keys=True)[:400]
    if name == "replace_lines":
        return (
            f"repl|{args.get('path')}|{args.get('start_line')}-"
            f"{args.get('end_line')}|{args.get('new_content')}"
        )
    if name == "run_command":
        cmd = str(args.get("command") or "")
        if "build" in cmd or "test" in cmd or "lint" in cmd:
            return f"check|{cmd}"
    if name == "web_search":
        return f"search|{args.get('query')}"
    return f"{name}|{json.dumps(args, ensure_ascii=False, sort_keys=True)[:200]}"

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
    research_call_count = 0  # 连续 web_search/fetch_url 次数，中间没有真正去改代码
    grep_streak = 0  # 连续 grep_search 次数，用于识别"逐个属性瞎猜"
    # 本轮开始时的消息长度：中断时丢掉未完成的 assistant/tool 片段，保留用户消息
    start_len = len(messages)

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            content, tool_calls, reasoning, looped = chat_once(messages)

            if looped:
                reasoning_abort_count += 1
                if reasoning_abort_count > MAX_REASONING_ABORTS:
                    print(
                        paint(
                            f"✗ 模型连续 {reasoning_abort_count} 次陷入重复思考，已停止本轮。",
                            C.ERR,
                        )
                        + paint("可尝试换个说法、拆小任务，或换更大的模型。", C.DIM, C.STATUS)
                    )
                    print()
                    return
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[系统提示] 你刚才的思考陷入重复循环，已被中断。"
                            "禁止继续长篇分析或重复相同句子，直接给出下一步工具调用；"
                            "如果信息已经够用，直接给出简短结论。"
                        ),
                    }
                )
                continue

            if tool_calls:
                assistant_msg: dict = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
                if reasoning:
                    assistant_msg["reasoning_content"] = reasoning
                messages.append(assistant_msg)

                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw_args = tc["function"].get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                        result = f"ERROR: invalid JSON arguments: {raw_args}"
                        show_tool_call(name, args)
                        show_tool_result(result)
                    else:
                        sig = tool_call_signature(name, args)
                        # Block identical mutating edits looping
                        if (
                            name in {"edit_file", "multi_edit", "replace_lines", "insert_lines", "delete_lines"}
                            and recent_sigs.count(sig) >= 1
                        ):
                            result = (
                                "ERROR: 重复操作 — 完全相同的编辑已经执行过一次，禁止再原样重试。"
                                "先用 read_file 确认文件当前内容（很可能已经生效，或者 old_text/行号已经不对了），"
                                "再决定下一步；如果是同一个报错反复修不好，才需要 web_search 报错原文换思路。"
                            )
                            show_tool_call(name, args)
                            show_tool_result(result)
                        else:
                            show_tool_call(name, args)
                            if not ask_tool_approval(name, args):
                                result = "ERROR: user denied tool execution"
                                show_tool_result(result)
                            else:
                                result = execute_tool(name, args)
                                show_tool_result(result)
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
                                    hint = (
                                        f"\n\nLOOP_HINT: 同一报错已经出现 {same} 次：\n  {fp}\n"
                                        "下一步必须：web_search（查询词=这条报错原文 + 项目实际用的框架/库名），"
                                        "再 fetch_url 打开一个相关结果；禁止重复刚才的改法。"
                                    )
                                    result = result + hint
                                    searched_this_error = True
                                if same >= 3:
                                    hint2 = (
                                        "\n\nESCALATE: 本地小修已经反复失败。"
                                        "请基于已经搜到的资料，用项目实际框架/库的已知正确写法"
                                        "重写这个文件/组件里出问题的部分，禁止再对同几行做微调。"
                                    )
                                    result = result + hint2

                        if name == "web_search":
                            searched_this_error = True

                        # 搜索/抓取次数堆积但一直没落地改代码 → 强制收敛，别再换个说法接着搜
                        if name in {"web_search", "fetch_url"}:
                            research_call_count += 1
                            if research_call_count >= 3:
                                result = result + (
                                    f"\n\nSTOP_SEARCHING: 已经调用了 {research_call_count} 次 "
                                    "web_search/fetch_url，还没有真正改代码。"
                                    "禁止再搜索或换个说法重新搜索，必须直接根据已拿到的信息"
                                    "用 edit_file/replace_lines 对文件做一次具体修改，"
                                    "再用 run_command 重新跑检查/构建看结果。"
                                )
                        elif name in {
                            "edit_file",
                            "multi_edit",
                            "replace_lines",
                            "insert_lines",
                            "delete_lines",
                            "write_file",
                        }:
                            research_call_count = 0

                        # 连续 grep_search（常见于无目的枚举）→ 提醒回到报错本身
                        if name == "grep_search":
                            grep_streak += 1
                            if grep_streak >= 3:
                                result = result + (
                                    f"\n\nSTOP_GUESSING: 已经连续 {grep_streak} 次 grep_search，这是在瞎猜。"
                                    "禁止再无目的枚举关键词；必须回到报错信息里的文件名/行号/标识符，"
                                    "用 read_file 读上下文，或 web_search 搜完整报错原文。"
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
                print()
                continue

            # final answer（走到这里说明没有 tool_calls）
            if content:
                messages.append({"role": "assistant", "content": content})
            else:
                print(paint("(empty response)", C.DIM, C.STATUS))
            print()
            return

        print(
            paint("✗ 工具轮次达到上限", C.ERR)
            + paint(f"（{MAX_TOOL_ROUNDS}），请再发一条消息让模型继续", C.DIM, C.STATUS)
        )
    except KeyboardInterrupt:
        sys.stdout.write(C.RESET + "\n")
        print(paint("⚠ 已取消当前任务（提示符下再按 Ctrl+C 退出）", C.ERR))
        print()
        del messages[start_len:]
        config.AUTO_APPROVE = False
        flush_input_buffer()

