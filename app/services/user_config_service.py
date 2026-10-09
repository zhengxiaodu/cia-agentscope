"""用户配置融合与缓存：YAML + mng 外部意图 + 权限过滤 + Redis 读写。

从 orchestrator_service.py 外移的纯函数模块：
- 登录时（auth 路由）：fuse → 写 Redis
- 会话时（orchestrator run）：读 Redis，未命中走 base-only 兜底
"""
import json
import logging

import yaml

from app.config import (
    AGENT_CONFIG_PATH,
    INTENT_CONFIG_PATH,
    SKILL_CONFIG_PATH,
    EXTERNAL_SKILLS_DIR,
    JWT_REFRESH_EXPIRE_DAYS,
)
from app.agents.registry import load_agent_definitions
from app.intent.recognizer import load_intent_config
from app.services.mng_service import (
    build_agent_definition_map,
    fetch_external_intents,
    merge_external_into_memory,
)

logger = logging.getLogger(__name__)

# Redis key：用户融合后的配置（登录时写入，会话时读取）
_REDIS_KEY_USER_CONFIG = "user_config:{user_id}"
_USER_CONFIG_TTL = 3600 * 24 * JWT_REFRESH_EXPIRE_DAYS   # 与 user_permissions 同 TTL


async def fuse_user_config(jwt_token: str, permissions: dict) -> dict:
    """步骤 1-4：加载 YAML + 请求 mng 外部意图 + 权限过滤 + 合并。

    返回可 JSON 序列化的 dict，供登录时写入 Redis。
    mng 请求失败仅记日志，降级为只用基础配置。
    """
    # ---- 1. 加载基础配置到内存 ----
    base_agent_defs = load_agent_definitions(AGENT_CONFIG_PATH)
    base_intents_raw = load_intent_config(INTENT_CONFIG_PATH)

    with open(SKILL_CONFIG_PATH, "r", encoding="utf-8") as f:
        base_skill_config = yaml.safe_load(f)
    base_skills = base_skill_config.get("skills", [])

    # ---- 2-3. 请求 mng 获取外部意图 ----
    external_intents = []
    if jwt_token:
        try:
            external_intents = await fetch_external_intents(jwt_token)
        except Exception:
            logger.exception(
                "[OrchestratorService] 登录时获取外部意图失败，仅用基础配置"
            )
            external_intents = []

    # ---- 4. 权限过滤 + 合并配置 ----
    merged_intents, merged_agents, merged_skills = merge_external_into_memory(
        base_intents=base_intents_raw.get("intents", []),
        base_agents=[a.model_dump() for a in base_agent_defs],
        base_skills=base_skills,
        external_intents=external_intents,
        permissions=permissions or {},
        external_skills_dir=EXTERNAL_SKILLS_DIR,
    )
    return {
        "merged_intents": merged_intents,
        "merged_agents": merged_agents,
        "merged_skills": merged_skills,
        "default_orchestration": base_intents_raw.get("default_orchestration", {}),
        # agent_id → 意图 definition 映射，供登录接口为
        # agent_access 注入 description（取自 /api/intents 原始返回）
        "agent_definitions": build_agent_definition_map(external_intents),
    }


async def build_and_cache_user_config(
    user_id: str,
    jwt_token: str,
    permissions: dict,
    redis_client,
) -> dict:
    """登录时融合（YAML + mng 外部意图 + 权限过滤）并写入 Redis。

    供 /chat 会话时直接读取。失败不阻断登录：mng 不可用或 Redis
    写失败均仅记日志，会话时读取不到缓存则走 base-only 兜底。

    Returns:
        融合后的配置 dict（含 agent_definitions 映射，供登录接口
        为 agent_access 注入 description）；fuse_user_config 异常时
        由调用方兜底（该异常不在此吞掉，由 auth 侧 try/except 处理）。
    """
    fused = await fuse_user_config(jwt_token, permissions)
    if redis_client is not None and user_id:
        try:
            key = _REDIS_KEY_USER_CONFIG.format(user_id=user_id)
            await redis_client.set(
                key,
                json.dumps(fused, ensure_ascii=False).encode("utf-8"),
                ex=_USER_CONFIG_TTL,
            )
            logger.info(f"[OrchestratorService] 用户配置已缓存: {key}")
        except Exception:
            logger.exception(
                f"[OrchestratorService] 缓存用户配置失败 user={user_id}"
            )
    return fused


async def load_cached_user_config(user_id: str, redis_client) -> dict | None:
    """会话时从 Redis 读取登录时缓存的融合配置。不存在或失败返回 None。"""
    if not user_id or redis_client is None:
        return None
    try:
        key = _REDIS_KEY_USER_CONFIG.format(user_id=user_id)
        raw = await redis_client.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except Exception:
        logger.exception(
            f"[OrchestratorService] 读取用户配置缓存失败 user={user_id}"
        )
        return None


async def load_config_bundle(user_id: str, redis_client) -> dict:
    """读取登录时缓存的融合配置（步骤 1-4 的产物），未命中则 base-only 兜底。

    缓存命中 → 直接用登录时融合好的 merged_intents/agents/skills；
    缓存未命中 → base-only 兜底融合（不请求 mng，无外部意图），
    外部意图在下次登录后恢复。会话路径永不发起 mng HTTP 调用。
    """
    fused = await load_cached_user_config(user_id, redis_client)
    if fused is not None:
        return {
            "merged_intents": fused.get("merged_intents", []),
            "merged_agents": fused.get("merged_agents", []),
            "merged_skills": fused.get("merged_skills", []),
            "default_orchestration": fused.get("default_orchestration", {}),
        }
    # 缓存未命中兜底：base-only 融合（不请求 mng，无外部意图）
    logger.warning(
        f"[OrchestratorService] 用户配置缓存未命中 user={user_id}，"
        f"走 base-only 兜底（无外部意图），下次登录后恢复"
    )
    return await fuse_user_config(jwt_token="", permissions={})
