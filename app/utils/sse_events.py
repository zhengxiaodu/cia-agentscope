"""SSE 阶段状态事件（stage_status）构造工具。

用于向前端提示当前执行到的环节（输入安全检验 / 获取工作区 / 创建工作区 /
输出内容最后检验等），前端可据 status=started/done 显示或清除进度提示。
"""
import json


class Stage:
    """stage_status 事件的阶段标识（前后端契约，勿随意改名）。"""

    # 输入内容安全检验（用户提问送检，创建工作区之前）
    INPUT_SENSITIVE_CHECK = "input_sensitive_check"
    # 获取工作区（命中已有沙箱/容器）
    WORKSPACE_GET = "workspace_get"
    # 创建工作区（首次/已过期，需新建沙箱/容器）
    WORKSPACE_CREATE = "workspace_create"
    # 输出内容最后检验（编排流结束后的 final_output 送检）
    OUTPUT_SENSITIVE_CHECK = "output_sensitive_check"


def stage_status_event(stage: str, status: str, message: str = "") -> str:
    """构造阶段状态 SSE 事件字符串。

    Args:
        stage: 阶段标识，见 Stage 常量。
        status: "started"（开始，前端可显示提示）/ "done"（完成，前端清除提示）。
        message: 人类可读提示文案。

    Returns:
        "data: {...}\\n\\n" 格式的 SSE 事件字符串。
    """
    payload = {
        "type": "stage_status",
        "stage": stage,
        "status": status,
        "message": message,
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
