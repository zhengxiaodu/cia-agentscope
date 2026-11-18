import hashlib
import logging
import sys
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.config import LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST

logger = logging.getLogger(__name__)


def derive_trace_id(biz_id: str) -> str:
    """业务 id → langfuse 要求的 32 位小写 hex trace id。

    确定性派生（与制度问答 regulations.providers.langfuse._langfuse_id 同一算法）：
    同一业务 id 恒得同一 trace_id，反馈 score 无需状态存储即可挂回原 trace。
    """
    return hashlib.sha256(biz_id.encode("utf-8")).hexdigest()[:32]


class LangfuseService:
    def __init__(self) -> None:
        self._enabled = False
        self._client = None

        if LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY:
            self._try_init()

    def _try_init(self) -> None:
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=LANGFUSE_PUBLIC_KEY,
                secret_key=LANGFUSE_SECRET_KEY,
                host=LANGFUSE_HOST,
            )
            self._enabled = True
            logger.info("Langfuse client initialized (host: %s)", LANGFUSE_HOST)
        except Exception as e:
            logger.warning("Failed to initialize Langfuse client: %s", e)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start_observation(
        self,
        name: str = "chat-response",
        as_type: str = "span",
        input: Any = None,
        model: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[Any]:
        if not self._enabled or not self._client:
            return None
        try:
            kwargs: dict = {"name": name, "as_type": as_type, "input": input}
            if model:
                kwargs["model"] = model
            if metadata:
                kwargs["metadata"] = metadata
            return self._client.start_observation(**kwargs)
        except Exception as e:
            logger.warning("Langfuse start_observation failed: %s", e)
            return None

    def end_observation(
        self,
        observation,
        output: Any = None,
        usage_details: Optional[dict] = None,
        level: Optional[str] = None,
        status_message: Optional[str] = None,
        input: Any = None,
    ) -> None:
        if not self._enabled or not observation:
            return
        try:
            kwargs: dict = {"output": output}
            if input is not None:
                kwargs["input"] = input
            if usage_details:
                kwargs["usage_details"] = usage_details
            if level:
                kwargs["level"] = level
            if status_message:
                kwargs["status_message"] = status_message
            observation.update(**kwargs)
            observation.end()
        except Exception as e:
            logger.warning("Langfuse end_observation failed: %s", e)

    def flush(self) -> None:
        if not self._enabled or not self._client:
            return
        try:
            self._client.flush()
        except Exception as e:
            logger.warning("Langfuse flush failed: %s", e)

    @contextmanager
    def start_span(self, name: str, input: Any = None, trace_id: Optional[str] = None) -> Iterator[Optional[Any]]:
        """启动一个 span（context manager，自动嵌套到当前活跃 observation 下）。

        用 v3 SDK 的 start_as_current_observation，借助 OTel context
        propagation 使子 span 自动成为当前活跃 span 的 child。
        根 span 与子 span 均用此方法；根 span 需保持活跃（with 包裹主体），
        其内启动的子 span 才能正确嵌套。

        trace_id（仅根 span 传，对齐制度问答 kb_search 模式）：
        显式 TraceContext 使 SDK 打上 AS_ROOT 标记，span 直接映射为
        trace 记录本体（name/input/output 随 span 属性落库），不依赖
        服务端对"无父 span → trace"的推断——该推断对版本敏感，裸根
        span 在部分环境会退化成 unnamed trace（name/input/output 全空）。
        子 span 不传 trace_id，自动嵌套在当前根观测下（否则会被标
        AS_ROOT 而误建出独立 trace）。

        用法：
            with langfuse_service.start_span("query-rewrite", input={...}) as span:
                ...
                if span:
                    span.update(output={...})
        未启用 langfuse 时 yield None，不报错，调用方需做 None 判断。
        """
        if not self._enabled or not self._client:
            yield None
            return
        # 仅兜底 span 的"创建"失败；with 体（yield 处）抛出的业务异常必须
        # 原样透传——@contextmanager 的生成器在 throw() 后再次 yield 会触发
        # Python 3.13 的 "generator didn't stop after throw()"，吞掉真实错误
        try:
            kwargs: dict = {"as_type": "span", "name": name, "input": input}
            if trace_id:
                from langfuse.types import TraceContext
                kwargs["trace_context"] = TraceContext(trace_id=trace_id)
            cm = self._client.start_as_current_observation(**kwargs)
            obs = cm.__enter__()
        except Exception as e:
            logger.warning("Langfuse start_span failed: %s", e)
            yield None
            return
        # 正常关闭必须由创建 span 的消费任务完成。OTel 通常在 detach
        # 内部捕获并记录错误，外层 except 无法拦截该日志；这里仅兼容
        # 会向外抛出跨 Context ValueError 的实现，不保证观测已经落库。
        try:
            yield obs
        finally:
            try:
                cm.__exit__(*sys.exc_info())
            except ValueError as exc:
                if "was created in a different Context" not in str(exc):
                    raise
                logger.warning(
                    "[langfuse_service] span 退出时上下文还原失败"
                    f"（跨 Context detach，已忽略）name={name}"
                )

    def create_score(
        self,
        trace_id: str,
        name: str = "user_feedback",
        value: bool = True,
        comment: Optional[str] = None,
    ) -> bool:
        if not self._enabled or not self._client:
            return False
        try:
            self._client.create_score(
                name=name,
                value=1.0 if value else 0.0,
                trace_id=trace_id,
                data_type="BOOLEAN",
                comment=comment,
            )
            return True
        except Exception as e:
            logger.warning("Langfuse create_score failed: %s", e)
            return False


# ---- 模块级单例访问器 ----

_current_instance: Optional["LangfuseService"] = None


def set_current_langfuse(svc: "LangfuseService") -> None:
    """在 main.py lifespan 中注册，供 chat_complete 等不便无法逐层传参的场景使用。"""
    global _current_instance
    _current_instance = svc


def get_current_langfuse() -> Optional["LangfuseService"]:
    """取当前已注册的 LangfuseService 实例，未注册返回 None。"""
    return _current_instance
