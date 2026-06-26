from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    """健康检查：返回编排服务和已加载的智能体数量。"""
    orchestrator = getattr(request.app.state, "orchestrator_service", None)
    if orchestrator and orchestrator.registry:
        agent_count = len(orchestrator.registry.definitions)
        return {"status": "healthy", "agents_loaded": agent_count}
    return {"status": "healthy", "agents_loaded": 0}


@router.get("/agents")
async def list_agents(request: Request):
    """列出所有已注册的智能体及其绑定的技能。"""
    orchestrator = getattr(request.app.state, "orchestrator_service", None)
    if not orchestrator or not orchestrator.registry:
        raise HTTPException(status_code=500, detail="Orchestrator service not loaded")

    agents = []
    for agent_id, definition in orchestrator.registry.definitions.items():
        agents.append({
            "id": definition.id,
            "name": definition.name,
            "skills": definition.skills,
        })
    return {"agents": agents}
