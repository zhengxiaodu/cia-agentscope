"""验证事件能以 UTF-8 发送，且 Unicode 兜底不吞掉其他序列化错误。"""

import json
import unittest
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_serializer
from pydantic_core import PydanticSerializationError

from app.utils.event_serialization import sanitize_surrogates, serialize_agent_event

# 孤立代理字符统一用 chr() 运行时构造：禁止在源码中书写 \u 转义的
# 代理字面量（如反斜杠 uD83D），普通字符串里转义会实际生效，Python
# 3.13 编译期序列化常量即抛 UnicodeEncodeError，导致模块无法 import
_HI = chr(0xD83D)   # 孤立高代理（emoji 代理对的上半）
_LO = chr(0xDE00)   # 孤立低代理（emoji 代理对的下半）
_LO2 = chr(0xDC00)  # 孤立低代理（无配对半）


class Event(BaseModel):
    delta: str
    metadata: dict[str, Any] = {}


class EventSerializationTests(unittest.TestCase):
    def test_normal_chinese_and_emoji_keep_original_json(self):
        event = Event(delta="中文😀")
        self.assertEqual(serialize_agent_event(event), event.model_dump_json())

    def test_surrogate_can_be_transmitted_without_mutating_event(self):
        event = Event(delta="hello" + _HI)
        payload = serialize_agent_event(event).encode("utf-8")
        self.assertEqual(json.loads(payload)["delta"], "hello" + _HI)
        self.assertEqual(event.delta, "hello" + _HI)

    def test_nested_surrogates_and_json_mode_types(self):
        event = Event(delta="ok", metadata={
            "items": [{"text": _LO2}],
            "time": datetime(2026, 10, 22, 12, 0),
        })
        payload = json.loads(serialize_agent_event(event).encode("utf-8"))
        self.assertEqual(payload["metadata"], {
            "items": [{"text": _LO2}], "time": "2026-10-22T12:00:00",
        })

    def test_split_surrogate_pair_keeps_both_halves(self):
        chunks = [Event(delta=_HI), Event(delta=_LO)]
        received = "".join(
            json.loads(serialize_agent_event(event).encode("utf-8"))["delta"]
            for event in chunks
        )
        self.assertEqual(received.encode("utf-16-le", "surrogatepass").decode("utf-16-le"), "😀")

    def test_fallback_keeps_json_only_serializer(self):
        class CustomEvent(Event):
            @field_serializer("metadata", when_used="json")
            def serialize_metadata(self, value):
                return {"wrapped": value}

        event = CustomEvent(delta=_HI, metadata={"id": 1})
        payload = json.loads(serialize_agent_event(event))
        self.assertEqual(payload["metadata"], {"wrapped": {"id": 1}})

    def test_unrelated_serialization_error_is_not_hidden(self):
        event = Event(delta="ok", metadata={"unknown": object()})
        with self.assertRaises(PydanticSerializationError):
            serialize_agent_event(event)


class SanitizeSurrogatesTests(unittest.TestCase):
    def test_clean_text_untouched(self):
        self.assertEqual(sanitize_surrogates("中文😀ok"), "中文😀ok")
        self.assertEqual(sanitize_surrogates(None), None)
        self.assertEqual(sanitize_surrogates(42), 42)

    def test_adjacent_surrogate_pair_combines_to_emoji(self):
        # 跨 delta 累积到同一字符串后的相邻代理对：还原为完整 emoji
        combined = _HI + _LO
        self.assertEqual(sanitize_surrogates(combined), "\U0001F600")

    def test_lone_surrogate_replaced_with_replacement_char(self):
        self.assertEqual(sanitize_surrogates("a" + _HI + "b"), "a\ufffdb")

    def test_nested_structures_sanitized_recursively(self):
        payload = {
            "content": "x" + _HI,
            "parts": [{"content": _HI + _LO}, ["y" + _LO]],
        }
        cleaned = sanitize_surrogates(payload)
        self.assertEqual(cleaned["content"], "x\ufffd")
        self.assertEqual(cleaned["parts"][0]["content"], "\U0001F600")
        self.assertEqual(cleaned["parts"][1][0], "y\ufffd")

    def test_sanitized_result_is_persistable(self):
        # 模拟落库链路：json.dumps(ensure_ascii=False) + pymysql UTF-8 编码
        final_output = "好的" + _HI + _LO + "收到"
        content_parts = [{"type": "text", "content": final_output + _LO}]
        for cleaned in (sanitize_surrogates(final_output), sanitize_surrogates(content_parts)):
            json.dumps(cleaned, ensure_ascii=False).encode("utf-8")  # 不应抛 UnicodeEncodeError


if __name__ == "__main__":
    unittest.main()
