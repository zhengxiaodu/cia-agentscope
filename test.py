import os
import yaml
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any, AsyncGenerator

from agentscope.agent import Agent
from agentscope.model import OpenAIChatModel
from agentscope.credential import OpenAICredential
from agentscope.message import AssistantMsg, UserMsg
from agentscope.event import AgentEvent
from agentscope.workspace import LocalWorkspace
from agentscope.state import AgentState
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.tool import Toolkit
from agentscope.event import (
    ReplyStartEvent,
    ReplyEndEvent
)

# 加载.env文件中的环境变量
load_dotenv()
SKILL_CONFIG_PATH = "config/skill_config.yml"
MODEL_CONFIG_PATH = "config/model_config.yml"


class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    session_id: str = None
    user_id: str = None


class ChatResponse(BaseModel):
    role: str
    content: str
    session_id: str = None


def load_model_config(config_path: str) -> dict:
    """加载模型配置"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def load_skills(config_path: str) -> Toolkit:
    """加载技能配置，初始化Toolkit"""
    skill_loaders = []

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    for skill in config.get("skills", []):
        skill_loaders.append(skill["directory"])

    workspace = LocalWorkspace(
        workdir="./my-workspace",
        default_mcps=[],
        skill_paths=skill_loaders,
    )
    await workspace.initialize()

    return Toolkit(tools=await workspace.list_tools(), skills_or_loaders=await workspace.list_skills())


def create_model_from_config(model_config: dict):
    """根据配置创建模型实例"""
    provider = model_config.get("provider", "openai")
    base_url = model_config.get("base_url", "https://api.deepseek.com/v1")
    model_name = model_config.get("model_name", "deepseek-chat")
    api_key = model_config.get("api_key", "OPENAI_API_KEY")
    parameters = model_config.get("parameters", {})

    if not api_key:
        raise ValueError(f"环境变量未设置")

    if provider == "openai":
        credential = OpenAICredential(api_key=api_key, base_url=base_url)
        model = OpenAIChatModel(
            credential=credential,
            model=model_name,
            stream=True,
            parameters=OpenAIChatModel.Parameters(**parameters),
        )
    else:
        raise ValueError(f"不支持的 provider: {provider}")

    return model


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.toolkit = await load_skills(SKILL_CONFIG_PATH)
    app.state.model = load_model_config(MODEL_CONFIG_PATH)
    print("Skills & model_cfg loaded successfully")
    yield


app = FastAPI(lifespan=lifespan)


async def generate_response(
        messages: List[Dict[str, Any]],
        session_id: str = None,
        user_id: str = None
) -> AsyncGenerator[str, None]:
    toolkit = app.state.toolkit
    model_config = app.state.model

    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not set")

    model_cfg = model_config.get("models", {}).get("default", {})
    agent_cfg = model_config.get("agent", {})
    # 创建模型实例
    model = create_model_from_config(model_cfg)

    # 创建 Agent
    agent = Agent(
        name=agent_cfg.get("name", "AI问答助手"),
        system_prompt=agent_cfg.get("system_prompt", ""),
        model=model,
        toolkit=toolkit,
        state=AgentState(
            permission_context=PermissionContext(
                mode=PermissionMode.BYPASS,
            )
        )

    )

    apply = None
    for msg in messages:
        content = msg.get("content", "")

        if isinstance(content, list):
            text_content = "\n".join([c.get("text", "") for c in content if isinstance(c, dict)])
        else:
            text_content = str(content)

        async for event in agent.reply_stream(UserMsg("user", text_content)):
            # 始终将事件追加到消息中
            if isinstance(event, ReplyStartEvent):
                apply = AssistantMsg(name=event.name, content=[], id=event.reply_id)
            elif isinstance(event, ReplyEndEvent):
                print(apply)

            if isinstance(event, AgentEvent):
                # 输出 AgentEvent 格式（与官方一致）
                apply.append_event(event)
                yield f"data: {event.model_dump_json()}\n\n"


@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        return StreamingResponse(
            generate_response(
                messages=request.messages,
                session_id=request.session_id,
                user_id=request.user_id
            ),
            media_type="text/event-stream"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    if hasattr(app.state, 'toolkit'):
        schemas = await app.state.toolkit.get_tool_schemas()
        return {"status": "healthy", "skills_loaded": len(schemas)}
    return {"status": "healthy", "skills_loaded": 0}


@app.get("/skills")
async def list_skills():
    if not hasattr(app.state, 'toolkit'):
        raise HTTPException(status_code=500, detail="Skills not loaded")

    schemas = await app.state.toolkit.get_tool_schemas()
    return {"skills": schemas}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
