"""单轮 /chat 请求的运行时状态。

OrchestratorService 是全局单例（app.state），此前每轮的 agent_ids /
success / token 计数 / 消费的上传文件 id 都写在单例实例属性上，并发
请求互相覆盖：典型症状是轮末绑定上传文件的 file_ids 被后启动的请求
清零，旧文件永远绑不上 message_id，下一轮与新上传文件一起重复注入
（用户可见："后一次提问用的是前一个文件"）。

现改为 generate_response 每轮创建独立 RoundState，经 run() 透传写入，
轮末持久化时直接读取——单例上不再持有任何请求级状态。
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class RoundState:
    """一轮对话请求范围内的运行时状态（每请求独立，并发安全）。"""

    # 本轮参与的 agent_id 列表（去重保序，随 assistant 消息落库）
    agent_ids: List[str] = field(default_factory=list)
    # 本轮编排是否成功（工作区失败 / agent 异常 / 编排结果失败为 False）
    success: bool = True
    # 本轮 MODEL_CALL_END 事件累积的真实 token 用量
    input_tokens: int = 0
    output_tokens: int = 0
    # 本轮实际消费的上传文件 id（轮末 bind_message_id 精确绑定用）
    consumed_upload_ids: List[int] = field(default_factory=list)
    # 已恢复历史文件的沙箱签名；未修改的恢复文件不重绑为本轮新产物。
    restored_files: dict[str, tuple[str, str]] = field(default_factory=dict)
    # 成功结束的写操作产生的版本，用于用户停止后的保守归档。
    completed_files: dict[str, tuple[str, str]] = field(default_factory=dict)
    # 仅保存在本轮内存中的快照，最终输出放行后提交。
    final_states: dict[str, dict] = field(default_factory=dict)
