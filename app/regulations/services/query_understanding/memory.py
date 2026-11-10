"""短期会话记忆与会话状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SessionMemory:
    """短期会话记忆 + 会话状态"""
    history: List[Dict[str, str]] = field(default_factory=list)
    current_policy: Optional[str] = None
    current_doc_id: Optional[str] = None
    confirmed_entities: Dict[str, List[str]] = field(default_factory=dict)
    user_id: Optional[str] = None
    max_history_turns: int = 6

    def add_turn(self, role: str, content: str):
        self.history.append({"role": role, "content": content})
        max_msgs = self.max_history_turns * 2
        if len(self.history) > max_msgs:
            self.history = self.history[-max_msgs:]

    def update_entities(self, entities: Dict[str, List[str]], doc_id: Optional[str] = None):
        for k, v in entities.items():
            if v:
                self.confirmed_entities[k] = v
        if entities.get("policy_name"):
            self.current_policy = entities["policy_name"][0]
        if doc_id:
            self.current_doc_id = doc_id

    def clear_topic(self):
        self.current_policy = None
        self.current_doc_id = None
        self.confirmed_entities = {}

    def get_history_text(self) -> str:
        if not self.history:
            return "无"
        return "\n".join([f"{t['role']}: {t['content']}" for t in self.history])
