"""mng 管理中心 API 调用服务。

提供从 mng 系统获取外部意图配置的能力，配合用户权限进行过滤。
"""
import logging
from typing import List, Optional

import httpx

from app.config import MNG_URL

logger = logging.getLogger(__name__)

# mng 系统意图接口
_MNG_INTENTS_PATH = "/api/intents"


async def fetch_external_intents(access_token: str) -> List[dict]:
    """从 mng 系统获取外部意图配置列表。

    请求 GET {MNG_URL}/api/intents，Header 中携带 access_token。
    失败不影响主流程，返回空列表。

    Args:
        access_token: 用户登录时 mng 返回的 access_token

    Returns:
        外部意图配置列表，mng 返回格式：
        [
            {
                "id": 123,
                "name": "生成PPT",
                "intentCode": "generate_ppt",
                "agents": [
                    {"id": "999", "name": "生成PPT智能体",
                     "code": "agent_ppt", "prompt": ""}
                ],
                "skills": [
                    {"id": "100", "name": "PPT生成skill",
                     "code": "skill_ppt"}
                ]
            }
        ]
    """
    if not MNG_URL:
        logger.warning("[mng_service] MNG_URL 未配置，跳过获取外部意图")
        return []
    if not access_token:
        logger.warning("[mng_service] access_token 为空，跳过获取外部意图")
        return []

    url = f"{MNG_URL}{_MNG_INTENTS_PATH}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                logger.warning(
                    f"[mng_service] 获取外部意图失败 HTTP {resp.status_code}"
                )
                return []

            body = resp.json()
            if body.get("code") != 200:
                logger.warning(
                    f"[mng_service] 获取外部意图业务失败: {body.get('message')}"
                )
                return []

            data = body.get("data", [])
            if not isinstance(data, list):
                logger.warning("[mng_service] 外部意图 data 格式异常（非列表）")
                return []
            return data
    except Exception:
        logger.exception("[mng_service] 调用 mng 获取外部意图异常，降级跳过")
        return []


def _build_whitelist_codes(agent_whitelist: list) -> set:
    """从 agent_whitelist 列表中提取所有 code，转为集合用于快速查找。

    Args:
        agent_whitelist: [{"id":"...", "name":"...", "code":"..."}, ...]
    """
    codes = set()
    if not isinstance(agent_whitelist, list):
        return codes
    for item in agent_whitelist:
        if isinstance(item, dict):
            code = item.get("code")
            if code:
                codes.add(code)
    return codes


def _build_blacklist_codes(skill_blacklist: list) -> set:
    """从 skill_blacklist 列表中提取所有 code，转为集合用于快速查找。"""
    codes = set()
    if not isinstance(skill_blacklist, list):
        return codes
    for item in skill_blacklist:
        if isinstance(item, dict):
            code = item.get("code")
            if code:
                codes.add(code)
    return codes


def merge_external_into_memory(
    base_intents: list,
    base_agents: list,
    base_skills: list,
    external_intents: list,
    permissions: dict,
    external_skills_dir: str,
) -> tuple:
    """将 mng 外部意图合并到基础配置中，并进行权限过滤。

    合并规则：
    - 外部 intent 的 id 用 intentCode 代替
    - 外部 agent 的 id 用 agent.code 代替
    - 外部 skill 的 directory 用 {external_skills_dir}/{skill.code}
    - 外部 agent 不在 agent_whitelist 中 → 跳过（同时取消关联）
    - 外部 skill 在 skill_blacklist 中 → 跳过

    Args:
        base_intents: 基础意图配置列表（从 intent_config.yml 加载）
        base_agents: 基础智能体配置列表（从 agent_config.yml 加载）
        base_skills: 基础技能配置列表（从 skill_config.yml 加载）
        external_intents: mng 返回的外部意图原始列表
        permissions: 用户权限 {"agent_whitelist": [...], "skill_blacklist": [...]}
        external_skills_dir: 外部技能根目录

    Returns:
        (merged_intents, merged_agents, merged_skills):
        - merged_intents: 基础意图 + 过滤后的外部意图
        - merged_agents: 基础智能体 + 过滤后的外部智能体
        - merged_skills: 基础技能 + 过滤后的外部技能
    """
    agent_whitelist = _build_whitelist_codes(
        permissions.get("agent_whitelist", []) if permissions else []
    )
    skill_blacklist = _build_blacklist_codes(
        permissions.get("skill_blacklist", []) if permissions else []
    )

    merged_intents = list(base_intents) if base_intents else []
    merged_agents = list(base_agents) if base_agents else []
    merged_skills = list(base_skills) if base_skills else []

    # 记录已经追加过的 agent/skill code，避免重复
    known_agent_ids = {a.get("id") for a in merged_agents if isinstance(a, dict)}
    known_skill_names = {s.get("name") for s in merged_skills if isinstance(s, dict)}

    # external_skills_dir 未配置时，外部 skill 无法加载，跳过全部外部意图
    skip_external_skills = not external_skills_dir

    for ext in (external_intents or []):
        if not isinstance(ext, dict):
            continue

        intent_name = ext.get("name", "")
        intent_code = ext.get("intentCode", "")
        if not intent_code:
            logger.warning(
                f"[mng_service] 外部意图缺少 intentCode: {intent_name}，跳过"
            )
            continue

        # 解析该意图关联的 agent
        agents_data = ext.get("agents", [])
        if not isinstance(agents_data, list) or not agents_data:
            logger.warning(
                f"[mng_service] 外部意图 {intent_code} 无关联 agent，跳过"
            )
            continue

        # 找到第一个在权限白名单中的 agent
        selected_agent = None
        for agent_item in agents_data:
            if not isinstance(agent_item, dict):
                continue
            agent_code = agent_item.get("code", "")
            if not agent_code:
                continue
            # 白名单检查
            if agent_code not in agent_whitelist:
                logger.info(
                    f"[mng_service] 外部 agent '{agent_code}' 不在白名单中，跳过"
                )
                continue
            selected_agent = agent_item
            break

        if selected_agent is None:
            logger.info(
                f"[mng_service] 外部意图 {intent_code} 无可用 agent"
                f"（白名单过滤后），跳过"
            )
            continue

        agent_code = selected_agent.get("code", "")
        agent_name = selected_agent.get("name", "")
        agent_prompt = selected_agent.get("prompt", "") or ""

        # 解析该意图关联的 skills
        skills_data = ext.get("skills", [])
        skill_codes = []  # 分配给该 agent 的 skill code 列表
        for skill_item in (skills_data or []):
            if not isinstance(skill_item, dict):
                continue
            skill_code = skill_item.get("code", "")
            if not skill_code:
                continue
            # 黑名单检查
            if skill_code in skill_blacklist:
                logger.info(
                    f"[mng_service] 外部 skill '{skill_code}' 在黑名单中，跳过"
                )
                continue

            # external_skills_dir 未配置时跳过 skill 加载（agent 仍可无工具运行）
            if skip_external_skills:
                logger.warning(
                    f"[mng_service] EXTERNAL_SKILLS_DIR 未配置，"
                    f"跳过外部 skill '{skill_code}'"
                )
                continue

            skill_codes.append(skill_code)

            # 构建 skill_config 格式，追加到技能列表
            skill_name = skill_code  # skill name 用 code
            if skill_name not in known_skill_names:
                known_skill_names.add(skill_name)
                merged_skills.append({
                    "name": skill_name,
                    "directory": f"{external_skills_dir}/{skill_code}",
                    "description": skill_item.get("name", skill_code),
                })

        # 构建 intent_config 格式（参考 intent_config.yml 中的 intent 条目）
        merged_intents.append({
            "id": intent_code,          # intent.id = intentCode
            "name": intent_name,
            "description": intent_name, # 外部意图没有独立 description，用 name
            "agent": agent_code,        # intent.agent = agent.code
        })

        # 构建 agent_config 格式（参考 agent_config.yml 中的 agent 条目）
        if agent_code not in known_agent_ids:
            known_agent_ids.add(agent_code)
            merged_agents.append({
                "id": agent_code,
                "name": agent_name,
                "skills": skill_codes,
                "system_prompt": agent_prompt,
            })

    logger.info(
        f"[mng_service] 外部意图合并完成："
        f"intents={len(merged_intents) - len(base_intents)}新增，"
        f"agents={len(merged_agents) - len(base_agents)}新增，"
        f"skills={len(merged_skills) - len(base_skills)}新增"
    )

    return merged_intents, merged_agents, merged_skills
