import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.intent.llm_client import create_async_client

router = APIRouter()

# 探测请求超时（秒）：探测场景要求快速返回，不沿用 SDK 默认的 600s
LLM_PROBE_TIMEOUT = 10.0


@router.get("/ping")
@router.get("/health")
async def health(request: Request):
    """健康检查：返回服务存活状态。

    仅报告进程可用（探针用途）；智能体配置已改为每次 run() 动态加载，
    启动时不再持有 registry，故不再返回 agents_loaded。
    """
    return "SUCCESSED"


@router.get("/agents")
async def list_agents(request: Request):
    """列出所有已注册的智能体及其绑定的技能。"""
    orchestrator = getattr(request.app.state, "orchestrator_service", None)
    # registry 已随动态加载重构移除；保留防御式访问，未加载时返回提示
    registry = getattr(orchestrator, "registry", None) if orchestrator else None
    if not registry:
        raise HTTPException(status_code=500, detail="Orchestrator service not loaded")

    agents = []
    for agent_id, definition in registry.definitions.items():
        agents.append({
            "id": definition.id,
            "name": definition.name,
            "skills": definition.skills,
        })
    return {"agents": agents}


def _llm_error(status_code: int, err_type: str, message: str,
               model: str, base_url: str) -> JSONResponse:
    """构造大模型探测失败响应：透传上游状态码与报错信息。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "error",
            "model": model,
            "base_url": base_url,
            "error": {
                "status_code": status_code,
                "type": err_type,
                "message": message,
            },
        },
    )


@router.get("/health/llm")
async def llm_health(request: Request):
    """探测大模型网关是否存活：带虚构 api_key 发最小请求，401 即存活。

    - 网关拒绝虚构鉴权（401）→ 存活，返回 200
    - 正常回复（200，网关未启用鉴权）→ 存活，返回 200 及模型回复
    - 其余错误：透传状态码；连接失败 / 超时：502 / 504
    """
    model_config = getattr(request.app.state, "model_config", None) or {}
    model_cfg = (model_config.get("models") or {}).get("default") or {}
    model_name = model_cfg.get("model_name", "deepseek-chat")
    base_url = model_cfg.get("base_url", "")
    if not model_cfg:
        return _llm_error(500, "config_error", "模型配置未加载（models.default 缺失）",
                          model_name, base_url)

    # 存活探测用虚构 key：不消耗配额，401 即证明网关存活
    probe_cfg = {**model_cfg, "api_key": "sk-healthcheck-invalid-key"}
    client = create_async_client(probe_cfg, timeout=LLM_PROBE_TIMEOUT)
    start = time.monotonic()
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=16,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            extra_headers={"app_serial_number": "OIA-AGENTSCOPE-HEALTH"},
        )
    except APITimeoutError as e:
        return _llm_error(504, "timeout", str(e), model_name, base_url)
    except APIStatusError as e:
        if e.status_code == 401:
            # 虚构鉴权被拒 → 网关存活
            return {"status": "ok", "model": model_name, "base_url": base_url}
        # 其余状态码（403/429/500 等）原样透传
        return _llm_error(e.status_code, type(e).__name__,
                          getattr(e, "message", None) or str(e),
                          model_name, base_url)
    except APIConnectionError as e:
        return _llm_error(502, "connection_error", str(e), model_name, base_url)
    except Exception as e:
        return _llm_error(502, type(e).__name__, str(e), model_name, base_url)

    latency_ms = int((time.monotonic() - start) * 1000)
    reply = ""
    if response.choices:
        reply = response.choices[0].message.content or ""
    return {
        "status": "ok",
        "model": model_name,
        "base_url": base_url,
        "latency_ms": latency_ms,
        "reply": reply,
    }
