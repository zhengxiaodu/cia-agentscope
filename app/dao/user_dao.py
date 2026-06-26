import os
import aiohttp

# 模拟账号数据, 生产环境应改为调用第三方管理系统的真实接口
_MOCK_USERS = {
    "zhangsan": {
        "password": "123456",
        "verification": True,
        "user_info": {"user_id": "123", "user_name": "小张", "department": "后勤部", "role": "普通用户"},
        "agent_access": ["制度问答"],
        "skills_blacklist": ["google"],
    },
    "admin": {
        "password": "123456",
        "verification": True,
        "user_info": {"user_id": "1", "user_name": "管理员", "department": "管理部", "role": "管理员"},
        "agent_access": ["制度问答", "通用问答"],
        "skills_blacklist": [],
    },
}


async def verify_login(username: str, password: str) -> dict:
    """验证登录凭据。

    当 AUTH_MOCK=true 时使用内置模拟数据; 否则请求 .env 中的 AUTH_API_URL。
    返回结构须为:
        {
            "verification": bool,
            "user_info": {"user_id", "user_name", "department", "role"},
            "agent_access": [...],
            "skills_blacklist": [...],
        }
    验证失败时仅返回 {"verification": False}。
    """
    if os.getenv("AUTH_MOCK", "true").lower() == "true":
        user = _MOCK_USERS.get(username)
        if user and user["password"] == password:
            return {
                "verification": True,
                "user_info": user["user_info"],
                "agent_access": user["agent_access"],
                "skills_blacklist": user["skills_blacklist"],
            }
        return {"verification": False}

    api_url = os.getenv("AUTH_API_URL")
    api_key = os.getenv("AUTH_API_KEY")
    if not api_url:
        return {"verification": False}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url,
                json={"username": username, "password": password},
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"verification": False}
    except Exception as e:
        print(f"[auth] 调用第三方认证服务失败: {e}")
        return {"verification": False}