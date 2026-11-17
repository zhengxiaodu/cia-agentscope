"""无需可选 SDK 依赖；执行生产保护代码及离线 SDK 的原始 reply 循环。"""
import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class Msg:
    def __init__(self, text="", role="assistant", reason="completed"):
        self.role, self.finished_reason = role, reason
        self.content = [NS(type="text", text=text)]


def load_guard():
    tree = ast.parse((ROOT / "app/utils/task_completion_guard.py").read_text(encoding="utf-8"))
    tree.body = [n for n in tree.body if not (
        isinstance(n, ast.ImportFrom) and n.module.startswith("agentscope")
    )]
    env = dict(Msg=Msg, MiddlewareBase=object, HintBlock=lambda **kw: NS(type="hint", **kw))
    exec(compile(tree, "task_completion_guard.py", "exec"), env)
    return NS(**env)


guard = load_guard()


def agent():
    return NS(
        name="agent",
        state=NS(reply_id="reply-1", cur_iter=0, context=[],
                 tool_context=NS(activated_groups=["active"])),
        react_config=NS(max_iters=200),
        toolkit=NS(get_tool_schemas=AsyncMock(return_value=[
            {"function": {"name": "Write"}}
        ])),
    )


async def consume(mw, ag, text, *, events=(), kwargs=None, message=None):
    msg = message or Msg(text)
    async def handler(**received):
        assert received == (kwargs or {})
        for event in events:
            yield event
        ag.state.context.append(Msg(text))
        yield msg
    return [item async for item in mw.on_reasoning(ag, kwargs or {}, handler)]


class DetectionTests(unittest.TestCase):
    def test_explicit_pending_execution(self):
        for text in [
            "Let me write the complete script.现在编写完整脚本。我将覆盖全部五个板块，共11页。",
            "接下来我将生成 PPT 文件。",
            "现在开始运行脚本。",
            "I will now generate the presentation.",
            "已经搜集资料。\n\n我现在来导出文件。",
        ]:
            with self.subTest(text=text):
                self.assertTrue(guard.ends_with_execution_promise(text))

    def test_answers_advice_offers_quotes_and_blockers(self):
        for text in [
            "下一步建议运行脚本生成 PPT。",
            "你可以执行 python gen_ppt.py。",
            "如果需要，我可以帮你生成 PPT。",
            "我将运行脚本，但缺少必要参数，请提供参数。",
            "I will generate it if you confirm.",
            "无法运行脚本，工具不可用。",
            "请确认后我将生成文件。",
            "最终文件已经生成。",
            "下面说明步骤：\n> 接下来我将运行脚本。",
            '模型回复“现在开始生成文件。”是提前结束的例子。',
            "```python\nprint('Let me write the script')\n```",
            "我将生成文件。\n\n已生成结果，请下载。",
            "我将运行脚本。已经生成并检查 PPT 文件。",
            "I will generate the file. I have generated the presentation.",
            "Let me know if you need a script.",
        ]:
            with self.subTest(text=text):
                self.assertFalse(guard.ends_with_execution_promise(text))


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_forwarded_once_and_hint_only_in_context(self):
        ag, mw = agent(), guard.TaskCompletionGuard()
        events = [NS(delta="plan"), NS(finished_reason="stop", usage=123)]
        out = await consume(mw, ag, "现在编写完整脚本。", events=events)
        self.assertEqual(out, events)
        self.assertEqual(len(ag.state.context), 1)
        self.assertEqual(ag.state.context[-1].content[-1].source, "task_completion_guard")
        ag.toolkit.get_tool_schemas.assert_awaited_once_with(["active"])
        self.assertEqual(ag.state.cur_iter, 0)
        self.assertEqual(ag.state.reply_id, "reply-1")

    async def test_normal_final_message_passes_without_tool_query(self):
        ag = agent()
        result = await consume(guard.TaskCompletionGuard(), ag, "分析结果如下。")
        self.assertEqual(len(result), 1)
        ag.toolkit.get_tool_schemas.assert_not_awaited()

    async def test_continuation_limit_and_new_reply_reset(self):
        ag, mw = agent(), guard.TaskCompletionGuard()
        for _ in range(2):
            self.assertEqual(await consume(mw, ag, "我将生成文件。"), [])
        with self.assertRaises(guard.IncompleteTaskError):
            await consume(mw, ag, "我将生成文件。")
        ag.state.reply_id = "reply-2"
        self.assertEqual(await consume(mw, ag, "我将生成文件。"), [])
        self.assertEqual(mw._continuations, 1)

    async def test_separate_agents_do_not_share_counter(self):
        first, second = guard.TaskCompletionGuard(), guard.TaskCompletionGuard()
        await consume(first, agent(), "我将生成文件。")
        self.assertEqual(second._continuations, 0)

    async def test_no_tools_or_compression_only_does_not_continue(self):
        for schemas in [[], [{"function": {"name": "CompressContext"}}]]:
            ag = agent()
            ag.toolkit.get_tool_schemas.return_value = schemas
            out = await consume(guard.TaskCompletionGuard(), ag, "我将生成文件。")
            self.assertEqual(len(out), 1)
            self.assertEqual(len(ag.state.context[-1].content), 1)

    async def test_tool_discovery_failure_preserves_response(self):
        ag = agent()
        ag.toolkit.get_tool_schemas.side_effect = RuntimeError("MCP offline")
        with self.assertLogs("builtins", level="WARNING"):
            self.assertEqual(len(await consume(
                guard.TaskCompletionGuard(), ag, "我将生成文件。")), 1)

    async def test_exhausted_budget_and_forced_finalization_do_not_continue(self):
        for exhausted in [False, True]:
            ag = agent()
            ag.state.cur_iter = 200 if exhausted else 10
            kwargs = {} if exhausted else {"tool_choice": NS(mode="none")}
            self.assertEqual(len(await consume(
                guard.TaskCompletionGuard(), ag, "我将生成文件。", kwargs=kwargs)), 1)
            ag.toolkit.get_tool_schemas.assert_not_awaited()

    async def test_interrupted_or_nonfinal_or_multimodal_message_passes(self):
        for msg in [Msg("我将生成文件。", reason="interrupted"),
                    Msg("我将生成文件。", role="user"),
                    Msg("我将生成文件。", reason="exceed_max_iters")]:
            self.assertEqual(len(await consume(
                guard.TaskCompletionGuard(), agent(), "", message=msg)), 1)
        msg = Msg("我将生成文件。")
        msg.content.append(NS(type="data"))
        self.assertEqual(len(await consume(
            guard.TaskCompletionGuard(), agent(), "", message=msg)), 1)
        events = [NS(finished_reason="interrupted")]
        self.assertEqual(len(await consume(
            guard.TaskCompletionGuard(), agent(), "我将生成文件。", events=events)), 2)

    async def test_handler_failure_and_cancellation_propagate(self):
        for error in [RuntimeError("model failure"), asyncio.CancelledError()]:
            async def handler(**kwargs):
                raise error
                yield
            with self.assertRaises(type(error)):
                _ = [x async for x in guard.TaskCompletionGuard().on_reasoning(
                    agent(), {}, handler)]


@dataclass
class Reasoning:
    hint: object = None
    tool_choice: object = None


@dataclass
class Acting:
    tool_calls: object = None


@dataclass
class Exit:
    exit_msg: object = None
    exit_events: object = None


class ReplyEndEvent:
    pass


class SDKLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_sdk_loop_continues_same_reply_without_replaying_tools(self):
        wheel = ROOT / "offline-bundle/python/wheels/agentscope-2.0.10-py3-none-any.whl"
        with zipfile.ZipFile(wheel) as archive:
            tree = ast.parse(archive.read("agentscope/agent/_agent.py").decode())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Agent")
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reply_impl")
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method]
        env = dict(asyncio=asyncio, Msg=Msg, Reasoning=Reasoning, Acting=Acting, Exit=Exit,
                   ReplyContext=lambda **kw: NS(**kw), _generate_id=lambda: "same-reply",
                   ReplyStartEvent=lambda **kw: NS(**kw), ReplyEndEvent=ReplyEndEvent,
                   _GenerateStructuredOutput=NS(name="structured"),
                   FinishedReason=NS(INTERRUPTED="interrupted"),
                   ToolResultState=NS(INTERRUPTED="interrupted"),
                   logger=NS(warning=lambda *a: None))
        for name in ["UserConfirmResultEvent", "UserInterruptEvent", "ExternalExecutionResultEvent",
                     "ModelCallEndEvent", "RequireUserConfirmEvent", "RequireExternalExecutionEvent",
                     "ToolResultEndEvent"]:
            env[name] = type(name, (), {})
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "sdk_reply.py", "exec"), env)
        ag, mw = agent(), guard.TaskCompletionGuard()
        ag.state.session_id = "session"
        ag._check_incoming_event = AsyncMock(return_value=False)
        ag._handle_incoming_messages = AsyncMock()
        ag.toolkit.remove_tool = AsyncMock()
        ag.compress_context = AsyncMock()
        ag.state.get_unfinished_tool_calls = lambda name: []
        ag._receive_reply_end = False
        tool_executions, reasoning_ids = [], []
        pending = []
        end = ReplyEndEvent()

        def next_action(final):
            if pending:
                return Acting(tool_calls=list(pending))
            if final:
                return Exit(exit_msg=final, exit_events=[end])
            return Reasoning()
        ag._next_action = next_action

        async def empty_stream():
            if False:
                yield
        ag._inject_runtime_state = empty_stream

        async def handler(**kwargs):
            reasoning_ids.append(ag.state.reply_id)
            if len(reasoning_ids) == 1:
                pending.append("write-script")
                ag.state.context.append(Msg("script written"))
            elif len(reasoning_ids) == 2:
                ag.state.context.append(Msg("现在开始运行脚本。"))
                yield Msg("现在开始运行脚本。")
            elif len(reasoning_ids) == 3:
                pending.append("run-script")
                ag.state.context.append(Msg("running"))
            else:
                ag.state.context.append(Msg("已生成并检查 PPTX 文件。"))
                yield Msg("已生成并检查 PPTX 文件。")
        def reasoning(**kwargs):
            return mw.on_reasoning(ag, kwargs, handler)
        ag._reasoning = reasoning
        ag._batch_tool_calls = AsyncMock(side_effect=lambda calls: [NS(type="sequential", tool_calls=calls)])

        async def execute(calls):
            tool_executions.extend(calls)
            pending.clear()
            yield NS(tool_result="success")
        ag._execute_sequential_tool_calls = execute
        out = []
        async for item in env["_reply_impl"](ag, inputs=Msg("生成 PPT")):
            out.append(item)
            if item is end:
                ag._receive_reply_end = True
        self.assertEqual(tool_executions, ["write-script", "run-script"])
        self.assertEqual(reasoning_ids, ["reply-1"] * 4)
        self.assertEqual(sum(item is end for item in out), 1)
        self.assertEqual(out[-1].content[0].text, "已生成并检查 PPTX 文件。")
        self.assertEqual(ag.state.cur_iter, 6)


if __name__ == "__main__":
    unittest.main()
