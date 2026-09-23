import json
import unittest
from unittest.mock import call, patch

from agent import deepseek, loop as agent_loop, model
from agent.deepseek import ContentFilter, Piece, parse_tool_calls
from agent.model import ChatResult


class ParseToolCallsTests(unittest.TestCase):
    def test_parses_dsml_parameters_as_openai_tool_calls(self) -> None:
        text = """准备读取
<｜DSML｜tool_calls>
<｜DSML｜invoke name="read_file">
<｜DSML｜parameter name="path" string="true">C:\\work\\a.py</｜DSML｜parameter>
<｜DSML｜parameter name="start_line" string="false">10</｜DSML｜parameter>
</｜DSML｜invoke>
<｜DSML｜invoke name="glob_search">
<｜DSML｜parameter name="pattern" string="true">**/*.py</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>"""

        visible, calls = parse_tool_calls(text)

        self.assertEqual(visible, "准备读取")
        self.assertEqual([call["function"]["name"] for call in calls], ["read_file", "glob_search"])
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"path": "C:\\work\\a.py", "start_line": 10},
        )
        self.assertEqual(calls[0]["id"], "call_1")
        self.assertEqual(calls[1]["id"], "call_2")

    def test_parses_v32_ascii_dsml_and_direct_json_body(self) -> None:
        text = (
            '<| DSML | function_calls><| DSML | invoke name="list_dir">'
            '{"path":"C:\\\\Users\\\\me"}'
            "</| DSML | invoke></| DSML | function_calls>"
        )

        visible, calls = parse_tool_calls(text)

        self.assertEqual(visible, "")
        self.assertEqual(calls[0]["function"]["name"], "list_dir")
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"path": "C:\\Users\\me"},
        )

    def test_parses_deepseek_web_short_calls_wrapper(self) -> None:
        text = (
            '<｜DSML｜calls><｜DSML｜invoke name="list_dir">'
            '<｜DSML｜parameter name="path" string="true">'
            "C:\\Users\\me"
            "</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜calls>"
        )

        visible, calls = parse_tool_calls(text)

        self.assertEqual(visible, "")
        self.assertEqual(calls[0]["function"]["name"], "list_dir")
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"path": "C:\\Users\\me"},
        )

    def test_parses_doubled_marker_with_spaces_and_parallel_calls(self) -> None:
        text = (
            '<｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name="check_syntax"> '
            '<｜｜DSML｜｜ parameter name="path" string="true">'
            "C:\\Users\\me\\page.html"
            "</｜｜DSML｜｜ parameter> </｜｜DSML｜｜ invoke> "
            '<｜｜DSML｜｜ invoke name="check_webpage"> '
            '<｜｜DSML｜｜ parameter name="path" string="true">'
            "C:\\Users\\me\\page.html"
            "</｜｜DSML｜｜ parameter> "
            '<｜｜DSML｜｜ parameter name="wait_ms" string="false">3000'
            "</｜｜DSML｜｜ parameter> </｜｜DSML｜｜ invoke> </｜｜DSML｜｜ calls>"
        )

        visible, calls = parse_tool_calls(text)

        self.assertEqual(visible, "")
        self.assertEqual(
            [call["function"]["name"] for call in calls],
            ["check_syntax", "check_webpage"],
        )
        self.assertEqual(
            json.loads(calls[1]["function"]["arguments"]),
            {"path": "C:\\Users\\me\\page.html", "wait_ms": 3000},
        )

    def test_keeps_existing_custom_format_compatible(self) -> None:
        text = '<tool_call>{"name":"list_dir","arguments":{"path":"."}}</tool_call>'

        visible, calls = parse_tool_calls(text)

        self.assertEqual(visible, "")
        self.assertEqual(calls[0]["function"]["name"], "list_dir")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "."})

    def test_leaves_malformed_dsml_visible(self) -> None:
        text = (
            '<｜DSML｜tool_calls><｜DSML｜invoke name="read_file">'
            '<｜DSML｜parameter name="start_line" string="false">ten'
            "</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>"
        )

        visible, calls = parse_tool_calls(text)

        self.assertEqual(visible, text)
        self.assertEqual(calls, [])
        self.assertTrue(deepseek.has_tool_call_markup(visible))


class ContentFilterTests(unittest.TestCase):
    def test_hides_chunked_dsml_and_preserves_surrounding_text(self) -> None:
        text = (
            "before"
            '<｜DSML｜tool_calls><｜DSML｜invoke name="list_dir">'
            '<｜DSML｜parameter name="path" string="true">.</｜DSML｜parameter>'
            "</｜DSML｜invoke></｜DSML｜tool_calls>"
            "after"
        )
        chunks = [text[:9], text[9:31], text[31:76], text[76:]]
        filt = ContentFilter()

        visible = "".join(filt.feed(chunk) for chunk in chunks) + filt.flush()

        self.assertEqual(visible, "beforeafter")
        self.assertTrue(filt.seen_tool)

    def test_hides_ascii_dsml_variant(self) -> None:
        text = (
            "before<| DSML | function_calls>"
            '<| DSML | invoke name="list_dir">{"path":"."}</| DSML | invoke>'
            "</| DSML | function_calls>after"
        )
        filt = ContentFilter()

        visible = filt.feed(text) + filt.flush()

        self.assertEqual(visible, "beforeafter")
        self.assertTrue(filt.seen_tool)

    def test_hides_short_calls_wrapper(self) -> None:
        text = (
            "before<｜DSML｜calls>"
            '<｜DSML｜invoke name="list_dir"></｜DSML｜invoke>'
            "</｜DSML｜calls>after"
        )
        chunks = [text[:12], text[12:30], text[30:]]
        filt = ContentFilter()

        visible = "".join(filt.feed(chunk) for chunk in chunks) + filt.flush()

        self.assertEqual(visible, "beforeafter")
        self.assertTrue(filt.seen_tool)

    def test_hides_doubled_marker_across_arbitrary_chunks(self) -> None:
        block = (
            '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="check_syntax">'
            '<｜｜DSML｜｜ parameter name="path" string="true">page.html'
            "</｜｜DSML｜｜ parameter></｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>"
        )
        text = "before" + block + "after"
        filt = ContentFilter()

        visible = "".join(filt.feed(text[i : i + 7]) for i in range(0, len(text), 7))
        visible += filt.flush()

        self.assertEqual(visible, "beforeafter")
        self.assertTrue(filt.seen_tool)


class DeepSeekRecoveryTests(unittest.TestCase):
    def test_recognizes_message_still_wip_payload(self) -> None:
        actual = json.dumps(
            {
                "code": 0,
                "data": {
                    "biz_code": 11,
                    "biz_msg": "message still wip",
                    "biz_data": None,
                },
            }
        )
        nested = json.dumps(
            {"code": 0, "data": {"biz_data": {"biz_code": 11, "biz_msg": "message still wip"}}}
        )

        self.assertTrue(deepseek._is_message_still_wip(actual))
        self.assertTrue(deepseek._is_message_still_wip(nested))
        self.assertFalse(deepseek._is_message_still_wip('{"code":0}'))

    def test_wip_response_retries_in_a_new_session_with_full_history(self) -> None:
        class FakeResponse:
            def __init__(self) -> None:
                self.lines = iter([b"event: close\n", b"data: [DONE]\n"])
                self.closed = False

            def readline(self) -> bytes:
                return next(self.lines, b"")

            def close(self) -> None:
                self.closed = True

        response = FakeResponse()
        messages = [{"role": "user", "content": "继续"}]
        with (
            patch.object(deepseek, "_plan_prompt", return_value=("pending only", True)),
            patch.object(deepseek, "_ensure_session", side_effect=["stuck", "fresh"]),
            patch.object(deepseek, "format_transcript", return_value="full history") as format_full,
            patch.object(
                deepseek,
                "_open_completion",
                side_effect=[deepseek._MessageStillWipError(), response],
            ) as open_completion,
            patch.object(deepseek.time, "sleep") as sleep,
        ):
            pieces = list(deepseek.stream_chat(messages))

        self.assertEqual(pieces, [])
        format_full.assert_called_once_with(messages, tools=True)
        self.assertEqual(
            open_completion.call_args_list,
            [
                call(
                    "stuck",
                    None,
                    "pending only",
                    thinking=deepseek.config.THINKING,
                    search=deepseek.config.DEEPSEEK_SEARCH,
                ),
                call(
                    "fresh",
                    None,
                    "full history",
                    thinking=deepseek.config.THINKING,
                    search=deepseek.config.DEEPSEEK_SEARCH,
                ),
            ],
        )
        sleep.assert_called_once_with(1.0)
        self.assertTrue(response.closed)

    def test_wip_inside_sse_also_retries(self) -> None:
        class FakeResponse:
            def __init__(self, lines: list[bytes]) -> None:
                self.lines = iter(lines)
                self.closed = False

            def readline(self) -> bytes:
                return next(self.lines, b"")

            def close(self) -> None:
                self.closed = True

        wip_payload = json.dumps(
            {"code": 0, "data": {"biz_code": 11, "biz_msg": "message still wip"}}
        ).encode()
        wip = FakeResponse([b"data: " + wip_payload + b"\n"])
        success = FakeResponse([b"data: [DONE]\n"])
        with (
            patch.object(deepseek, "_plan_prompt", return_value=("pending", True)),
            patch.object(deepseek, "_ensure_session", side_effect=["stuck", "fresh"]),
            patch.object(deepseek, "format_transcript", return_value="full"),
            patch.object(deepseek, "_open_completion", side_effect=[wip, success]),
            patch.object(deepseek.time, "sleep") as sleep,
        ):
            pieces = list(deepseek.stream_chat([{"role": "user", "content": "继续"}]))

        self.assertEqual(pieces, [])
        self.assertTrue(wip.closed)
        self.assertTrue(success.closed)
        sleep.assert_called_once_with(1.0)

    def test_reasoning_abort_discards_deepseek_session(self) -> None:
        class FakeRenderer:
            def start(self) -> None:
                pass

            def on_reasoning(self, _text: str) -> None:
                pass

            def abort(self) -> None:
                pass

            def finish(self) -> None:
                pass

        def pieces():
            yield Piece(reasoning="x" * 150)

        with (
            patch.object(model, "stream_chat", return_value=pieces()),
            patch.object(model, "StreamRenderer", return_value=FakeRenderer()),
            patch.object(model, "_detect_reasoning_loop", return_value=True),
            patch.object(model, "reset_deepseek_session") as reset,
            patch.object(model, "warn"),
        ):
            result = model._chat_once_deepseek([])

        self.assertTrue(result.looped)
        self.assertEqual(result.loop_reason, "repeat")
        reset.assert_called_once_with()

    def test_malformed_dsml_becomes_protocol_error_instead_of_final_answer(self) -> None:
        malformed = (
            "<｜DSML｜tool_calls>"
            '<｜DSML｜parameter name="path" string="true">page.html'
            "</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>"
        )

        class FakeRenderer:
            def start(self) -> None:
                pass

            def on_tool_calls(self) -> None:
                pass

            def finish(self) -> None:
                pass

            def abort(self) -> None:
                pass

        def pieces():
            yield Piece(content=malformed)

        with (
            patch.object(model, "stream_chat", return_value=pieces()),
            patch.object(model, "StreamRenderer", return_value=FakeRenderer()),
        ):
            result = model._chat_once_deepseek([])

        self.assertEqual(result.content, "")
        self.assertEqual(result.tool_calls, [])
        self.assertIn("工具调用标记不完整", result.tool_protocol_error)


class ReasoningLoopTests(unittest.TestCase):
    def test_detects_only_exact_repeated_suffix(self) -> None:
        repeated = ("This is a repeated reasoning segment with enough length. " * 5)
        normal = "".join(f"Step {i}: inspect a different part of the file. " for i in range(20))

        self.assertTrue(model._detect_reasoning_loop(repeated))
        self.assertFalse(model._detect_reasoning_loop(normal))

    def test_reports_length_limit_separately_from_repetition(self) -> None:
        class FakeRenderer:
            def start(self) -> None:
                pass

            def on_reasoning(self, _text: str) -> None:
                pass

            def abort(self) -> None:
                pass

            def finish(self) -> None:
                pass

        def pieces():
            yield Piece(reasoning="x" * 11)

        with (
            patch.object(model, "DEEPSEEK_MAX_REASONING_CHARS", 10),
            patch.object(model, "stream_chat", return_value=pieces()),
            patch.object(model, "StreamRenderer", return_value=FakeRenderer()),
            patch.object(model, "reset_deepseek_session"),
            patch.object(model, "_warn_reasoning_abort") as warning,
        ):
            result = model._chat_once_deepseek([])

        self.assertTrue(result.looped)
        self.assertEqual(result.loop_reason, "length")
        warning.assert_called_once_with("length", 11)

    def test_successful_round_resets_consecutive_abort_counter(self) -> None:
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "list_dir", "arguments": "{}"},
        }
        results = [
            ChatResult(looped=True, loop_reason="repeat"),
            ChatResult(tool_calls=[tool_call]),
            ChatResult(looped=True, loop_reason="repeat"),
            ChatResult(content="done"),
        ]
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "help"},
        ]

        with (
            patch.object(agent_loop, "MAX_REASONING_ABORTS", 2),
            patch.object(agent_loop, "chat_once", side_effect=results) as chat_once,
            patch.object(agent_loop.config, "API_STYLE", "openai"),
            patch.object(agent_loop.context, "check"),
            patch.object(agent_loop.context, "calibrate"),
            patch.object(agent_loop.context, "usage", return_value=(0, 1)),
            patch.object(agent_loop, "ask_tool_approval", return_value=(True, "")),
            patch.object(agent_loop, "execute_tool", return_value="ok"),
            patch.object(agent_loop, "show_tool_call"),
            patch.object(agent_loop, "show_tool_result"),
            patch.object(agent_loop, "show_turn_stats"),
            patch.object(agent_loop.console, "print"),
        ):
            agent_loop.run_agent_turn(messages)

        self.assertEqual(chat_once.call_count, 4)
        self.assertEqual(messages[-1], {"role": "assistant", "content": "done"})

    def test_malformed_tool_protocol_is_retried_not_saved_as_answer(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "continue"},
        ]
        results = [
            ChatResult(tool_protocol_error="malformed"),
            ChatResult(content="done"),
        ]

        with (
            patch.object(agent_loop, "chat_once", side_effect=results) as chat_once,
            patch.object(agent_loop.context, "check"),
            patch.object(agent_loop.context, "calibrate"),
            patch.object(agent_loop.context, "usage", return_value=(0, 1)),
            patch.object(agent_loop, "show_turn_stats"),
            patch.object(agent_loop, "warn") as warning,
            patch.object(agent_loop.console, "print"),
        ):
            agent_loop.run_agent_turn(messages)

        self.assertEqual(chat_once.call_count, 2)
        self.assertIn("工具调用格式残缺", warning.call_args.args[0])
        self.assertIn("请重新输出完整 DSML", messages[-2]["content"])
        self.assertEqual(messages[-1], {"role": "assistant", "content": "done"})

    def test_reasoning_only_response_is_retried_for_final_summary(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "finish the task"},
        ]
        results = [
            ChatResult(reasoning="Checks passed; I should summarize."),
            ChatResult(content="已完成修改，语法和页面运行检查均通过。"),
        ]

        with (
            patch.object(agent_loop, "chat_once", side_effect=results) as chat_once,
            patch.object(agent_loop.context, "check"),
            patch.object(agent_loop.context, "calibrate"),
            patch.object(agent_loop.context, "usage", return_value=(0, 1)),
            patch.object(agent_loop, "show_turn_stats"),
            patch.object(agent_loop, "warn") as warning,
            patch.object(agent_loop.console, "print"),
        ):
            agent_loop.run_agent_turn(messages)

        self.assertEqual(chat_once.call_count, 2)
        self.assertIn("正在要求其继续并总结", warning.call_args.args[0])
        self.assertIn("立即用简短中文总结", messages[-2]["content"])
        self.assertEqual(
            messages[-1],
            {"role": "assistant", "content": "已完成修改，语法和页面运行检查均通过。"},
        )


if __name__ == "__main__":
    unittest.main()
