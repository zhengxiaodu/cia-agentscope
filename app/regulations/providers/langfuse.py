"""Langfuse 可观测性客户端 — 适配 langfuse SDK 4.x observation API。

4.x 用 start_as_current_observation（上下文管理器）建观测：
- 根观测（trace）带 trace_context 设置 trace_id（AS_ROOT，符合根语义）；
- 子 span 不带 trace_context，自动嵌套在当前根观测下（否则会被标 AS_ROOT
  而误建出独立 trace）。

trace_id 必须是 32 位小写 hex，由业务 id（message_id/serial）经 sha256 派生，
保证反馈 score 可挂回同一 trace。session_id/user_id 用 propagate_attributes
设为 trace 一级属性。

配置从 app.regulations.config 读取（REGULATIONS_LANGFUSE_*），host 复用主项目
LANGFUSE_HOST；public/secret key 任一为空则 enabled=False，所有方法 no-op。
"""

from __future__ import annotations

import hashlib
import logging
import random
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import AsyncIterator

from langfuse import Langfuse, propagate_attributes
from langfuse.types import TraceContext

from app.config import LANGFUSE_HOST
from app.regulations.config import (
    REGULATIONS_LANGFUSE_ENABLED,
    REGULATIONS_LANGFUSE_PUBLIC_KEY,
    REGULATIONS_LANGFUSE_SAMPLE_RATE,
    REGULATIONS_LANGFUSE_SECRET_KEY,
)

logger = logging.getLogger(__name__)


def _langfuse_id(biz_id: str) -> str:
    """业务 id（msg_id/serial）→ langfuse 要求的 32 位小写 hex trace id。

    确定性：反馈 score 用同一函数从 message_id 派生，即可挂回同一 trace。
    """
    return hashlib.sha256(biz_id.encode("utf-8")).hexdigest()[:32]


# 当前请求是否有活跃根观测（ContextVar，供 KBClient 决定是否挂 retrieval 子 span）。
# start_trace 进入时置 True、退出时复位；asyncio 任务自动继承。
_current_trace_active: ContextVar[bool] = ContextVar("langfuse_trace_active", default=False)


def get_current_trace_active() -> bool:
    return _current_trace_active.get()


class LangfuseClient:
    """Langfuse 可观测性客户端。

    - 每个问答一条 trace（根观测），trace_id 由业务 id 派生。
    - session_id/user_id 通过 propagate_attributes 设为 trace 一级属性。
    - 通过 REGULATIONS_LANGFUSE_ENABLED / REGULATIONS_LANGFUSE_SAMPLE_RATE 控制；
      public/secret key 任一为空则禁用，全部 no-op。
    """

    def __init__(self):
        self.sample_rate = REGULATIONS_LANGFUSE_SAMPLE_RATE
        # 密钥任一为空则禁用（即使总开关为 true）
        self.enabled = (
            REGULATIONS_LANGFUSE_ENABLED
            and bool(REGULATIONS_LANGFUSE_PUBLIC_KEY)
            and bool(REGULATIONS_LANGFUSE_SECRET_KEY)
        )

        if self.enabled:
            self.client = Langfuse(
                public_key=REGULATIONS_LANGFUSE_PUBLIC_KEY,
                secret_key=REGULATIONS_LANGFUSE_SECRET_KEY,
                host=LANGFUSE_HOST,
            )
        else:
            self.client = None

    # ── trace / span（上下文管理器）────────────────────────

    @asynccontextmanager
    async def start_trace(
        self,
        trace_id: str,
        session_id: str,
        user_id: str,
        name: str | None = None,
        metadata: dict | None = None,
        input: dict | None = None,
    ) -> AsyncIterator[object | None]:
        """为一次问答建根观测（trace），作为异步上下文管理器。

        进入即建根观测并置为当前 span；退出自动 end。未启用/未采样/异常时
        yield None，调用方需容忍（score 仍按 trace_id 派生照常上报）。
        """
        if not self.enabled or not self.client or not self._should_sample():
            yield None
            return

        meta = {"mode": "chat"}
        if metadata:
            meta.update(metadata)

        try:
            obs_cm = self.client.start_as_current_observation(
                name=name or f"chat-{trace_id}",
                as_type="span",
                trace_context=TraceContext(trace_id=_langfuse_id(trace_id)),
                input=input,
                metadata=meta,
            )
        except Exception as exc:
            logger.warning(f"Langfuse start_trace 失败: {exc}")
            obs_cm = None

        if obs_cm is None:
            yield None
            return

        token = _current_trace_active.set(True)
        try:
            # obs_cm 与 propagate_attributes 都是 OTel 的同步上下文管理器
            # （只有 __enter__/__exit__），须用 with；context 由 contextvar 管理，
            # 跨 async 安全。propagate 让根观测及其子 span（retrieval）都继承
            # user_id/session_id。
            with propagate_attributes(user_id=user_id, session_id=session_id):
                with obs_cm as obs:
                    yield obs
        finally:
            _current_trace_active.reset(token)

    @asynccontextmanager
    async def start_span(self, name: str, input: dict | None = None) -> AsyncIterator[object | None]:
        """在当前根观测下建子 span（如 retrieval），作为异步上下文管理器。

        无活跃根观测/未启用/异常时 yield None，调用方需容忍。
        """
        if not self.enabled or not self.client or not _current_trace_active.get():
            yield None
            return

        try:
            obs_cm = self.client.start_as_current_observation(
                name=name, as_type="span", input=input,
            )
        except Exception as exc:
            logger.warning(f"Langfuse start_span 失败: {exc}")
            obs_cm = None

        if obs_cm is None:
            yield None
            return

        with obs_cm as span:
            yield span

    # ── score（反馈） ──────────────────────────────────────

    def score(self, trace_id: str, name: str, value: float | int | bool, comment: str = ""):
        """上报反馈/业务结果分数，挂回同一业务 id 派生的 trace。

        value 可为 float（如 user_feedback）或 bool（如 empty_answer）。
        """
        if not self.enabled or not self.client:
            return
        try:
            self.client.create_score(
                name=name,
                value=value,
                trace_id=_langfuse_id(trace_id),
                comment=comment,
            )
        except Exception as exc:
            logger.warning(f"Langfuse score 上报失败: {exc}")

    # ── helpers ──────────────────────────────────────────

    def _should_sample(self) -> bool:
        return random.random() < self.sample_rate

    def flush(self):
        """确保所有 pending 数据上报完成（shutdown 时调用）。"""
        if self.enabled and self.client:
            self.client.flush()


@lru_cache
def get_langfuse() -> LangfuseClient:
    """返回全局单例 LangfuseClient。"""
    return LangfuseClient()
