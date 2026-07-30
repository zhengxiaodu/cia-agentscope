import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.config import LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST

logger = logging.getLogger(__name__)


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
    ) -> Optional[Any]:
        if not self._enabled or not self._client:
            return None
        try:
            return self._client.start_observation(
                name=name,
                as_type=as_type,
                input=input,
            )
        except Exception as e:
            logger.warning("Langfuse start_observation failed: %s", e)
            return None

    def end_observation(
        self,
        observation,
        output: Any = None,
    ) -> None:
        if not self._enabled or not observation:
            return
        try:
            observation.update(output=output)
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
    def start_span(self, name: str, input: Any = None) -> Iterator[Optional[Any]]:
        """启动一个 span（context manager，自动嵌套到当前活跃 observation 下）。

        用 v3 SDK 的 start_as_current_observation，借助 OTel context
        propagation 使子 span 自动成为当前活跃 span 的 child。
        根 span 与子 span 均用此方法；根 span 需保持活跃（with 包裹主体），
        其内启动的子 span 才能正确嵌套。

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
        try:
            with self._client.start_as_current_observation(
                as_type="span", name=name, input=input,
            ) as obs:
                yield obs
        except Exception as e:
            logger.warning("Langfuse start_span failed: %s", e)
            yield None

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