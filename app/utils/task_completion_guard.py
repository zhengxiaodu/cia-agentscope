"""防止 ReAct 把明确的执行承诺当作最终答案；不以缺少工具调用判断失败。"""
import logging
import re
from typing import Any, AsyncGenerator, Callable

from agentscope.message import HintBlock, Msg
from agentscope.middleware import MiddlewareBase

logger = logging.getLogger(__name__)

TASK_COMPLETION_GUIDANCE = (
    "\n任务完成要求：用户要求执行操作或生成文件时，必须在本轮实际执行已授权的操作，"
    "再给出最终结果。不要以“接下来我将”“现在开始”“Let me…”等尚未执行的计划结束。"
    "用户要求 PPTX、DOCX、XLSX 等成品时，应生成对应成品并用工具确认文件存在且可读取；"
    "除非用户要求脚本，否则仅写生成脚本不代表完成。根据已有工具结果从未完成的步骤继续，"
    "不要重复已成功执行的操作。遇到工具不可用、执行失败、权限不足、缺少必要信息或需要"
    "用户确认时，如实说明已完成的部分、具体阻碍和需要用户提供的内容，不要虚构成功。"
)

_ACTION = r"(?:编写|撰写|写入|创建|生成|制作|导出|执行|运行|调用|读取|检查|查询|搜索|下载|上传|修改|保存|完成)"
_PROMISE = re.compile(
    rf"(?:^|[。！？.!?\n])\s*(?:现在|接下来|下一步)?\s*"
    rf"(?:我(?:将|会|要|来|准备|马上|现在|接下来|继续)|现在(?:开始|就|继续)?|接下来(?:开始|继续)?|继续|接着)"
    rf"[^。！？\n]{{0,35}}{_ACTION}"
    r"|(?:^|[.!?\n])\s*(?:let me|i (?:will|shall|am going to|will now))\s+"
    r"(?:\w+\s+){0,5}(?:write|create|generate|build|export|execute|run|call|read|check|search|download|upload|save|modify)\b",
    re.IGNORECASE,
)
_BLOCKER_OR_OFFER = re.compile(
    r"如果|若需|如需|可以帮|是否需要|请(?:先|提供|确认|批准)|等待|待你|无法|不能|不可用|"
    r"权限不足|缺少|失败|未能|if you|would you|let me know|waiting for|"
    r"cannot|can't|unable|unavailable|need your|please (?:provide|confirm|approve)",
    re.IGNORECASE,
)


def ends_with_execution_promise(text: str) -> bool:
    """只识别结尾的第一人称执行承诺，跳过代码、引用、条件建议和明确阻碍。"""
    text = re.sub(r"```[\s\S]*?(?:```|$)", "", text)
    text = re.sub(r"`[^`\n]*`", "", text)
    text = re.sub(r"[“\"「][^”\"」\n]*[”\"」]", "", text)
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    # 只检查末尾段落，避免正文中曾经描述计划、结尾已报告结果时误触发。
    tail = re.split(r"\n\s*\n", text.strip())[-1][-600:]
    sentences = re.split(r"(?<=[。！？.!?])\s*|\n", tail)
    tail = "\n".join(sentences[-3:])
    matches = list(_PROMISE.finditer(tail))
    if not matches or _BLOCKER_OR_OFFER.search(tail):
        return False
    # 同一段落也可能先回顾计划、再报告完成，只看最后一次承诺之后的内容。
    after_promise = tail[matches[-1].end():]
    return not re.search(
        r"已(?:经)?(?:完成|生成|执行|保存|导出|检查)|完成了|"
        r"\b(?:successfully|have (?:generated|created|saved|completed))\b",
        after_promise, re.IGNORECASE,
    )


class IncompleteTaskError(RuntimeError):
    """续执行仍只返回承诺，交给现有编排异常路径报告失败。"""


class TaskCompletionGuard(MiddlewareBase):
    """每个 Agent 独立计数，在同一 reply 内抑制候选 final Msg，让 SDK 继续推理。

    所有流事件原样透传；不重新调用 reply_stream，不重置迭代预算，不执行工具。
    """

    def __init__(self, max_continuations: int = 2):
        self.max_continuations = max_continuations
        self._reply_id = None
        self._continuations = 0

    async def on_reasoning(
        self, agent: Any, input_kwargs: dict, next_handler: Callable[..., AsyncGenerator]
    ) -> AsyncGenerator[Any, None]:
        if self._reply_id != agent.state.reply_id:
            self._reply_id = agent.state.reply_id
            self._continuations = 0
        interrupted = False
        async for item in next_handler(**input_kwargs):
            reason = getattr(item, "finished_reason", None)
            if reason == "interrupted":
                interrupted = True
            if (
                not isinstance(item, Msg)
                or item.role != "assistant"
                or interrupted
                or reason not in (None, "completed")
                or getattr(input_kwargs.get("tool_choice"), "mode", None) == "none"
                or agent.state.cur_iter >= agent.react_config.max_iters
                or any(getattr(b, "type", None) != "text" for b in item.content)
                or not ends_with_execution_promise(
                    "\n".join(getattr(b, "text", "") for b in item.content)
                )
            ):
                yield item
                continue

            # 使用当前激活工具组，排除 SDK 自动注入的压缩工具（纯问答也有该工具）。
            try:
                schemas = await agent.toolkit.get_tool_schemas(
                    agent.state.tool_context.activated_groups
                )
            except Exception:
                logger.warning("完成保护无法查询可用工具，放行原响应", exc_info=True)
                yield item
                continue
            if not any(s.get("function", {}).get("name") not in (None, "CompressContext")
                       for s in schemas):
                yield item
                continue
            if self._continuations >= self.max_continuations:
                raise IncompleteTaskError(
                    "任务未完成：模型多次只返回下一步计划，没有继续执行。请重试或明确剩余操作。"
                )
            self._continuations += 1
            logger.warning(
                "智能体 %s 最终响应仍为执行计划，续执行提醒 %d/%d（reply=%s）",
                agent.name, self._continuations, self.max_continuations, self._reply_id,
            )
            # SDK 已保存本次模型响应；追加提示即可，不删历史也不重放先前工具。
            agent.state.context[-1].content.append(HintBlock(
                source="task_completion_guard",
                hint=(
                    "你刚才只说明了尚未执行的下一步，任务还没有完成。请检查已有工具结果，"
                    "从尚未完成的步骤继续实际执行已授权任务，不要重复成功的操作。"
                    "如果需要生成成品文件，请生成并检查对应成品。若有具体阻碍或需要用户确认，"
                    "请明确说明；不要再次仅以计划结束。"
                ),
            ))
            # 不 yield 候选 Msg：SDK final_msg 保持 None，继续原 ReAct 循环。
