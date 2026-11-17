"""对话级 trace 流水号：全链路 LLM 请求统一携带 app_serial_number 请求头。

与业务智能体流式调用（create_model_from_config 的 client_kwargs）互补：
意图识别 / 查询改写 / ReAct 推理 / 并行汇总 / 推荐问题 / 输入围栏预检
走长生命周期的 AsyncOpenAI 客户端，客户端创建时不带 header，
由本模块的 ContextVar 在每次请求出口动态注入。

ContextVar 语义：generate_response 开头 set 一次
（OIA-AGENTSCOPE-{message_pair_id}），同任务上下文内的所有
下游调用（含 create_task 派生的子任务）自动继承，流结束无需
清理（本轮任务上下文随请求销毁）。
"""
import contextvars

_current_serial: contextvars.ContextVar[str] = contextvars.ContextVar(
    "app_serial_number", default=""
)


def set_serial(serial: str) -> None:
    """设置当前对话轮的流水号（generate_response 入口调用一次）。"""
    _current_serial.set(serial or "")


def get_serial() -> str:
    """读取当前流水号；未设置（非对话链路，如健康探测）返回空串。"""
    return _current_serial.get()
