import os
import aiohttp
from agentscope.tool import FunctionTool, ToolChunk

BOCHA_API_KEY = "sk-b2456820150d48a68c15a0f76ded1eef"
BOCHA_API_URL = "https://api.bocha.com/v1/search"

async def bocha_search(query: str) -> ToolChunk:
    """使用博查搜索获取网上的知识。

    Args:
        query: 搜索查询词
    """
    headers = {
        "Authorization": f"Bearer {BOCHA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "query": query,
        "limit": 5
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(BOCHA_API_URL, headers=headers, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get("results", [])
                    
                    if results:
                        content = "\n\n".join([
                            f"【{i+1}】{result.get('title', '')}\n{result.get('summary', '')}\n来源: {result.get('url', '')}"
                            for i, result in enumerate(results)
                        ])
                    else:
                        content = "未找到相关搜索结果。"
                else:
                    content = f"搜索失败，状态码: {response.status}"
    except Exception as e:
        content = f"搜索异常: {str(e)}"
    
    return ToolChunk(text=content)

bocha_search_tool = FunctionTool(
    func=bocha_search,
    name="bocha_search",
    description="使用博查搜索引擎获取网上的知识和最新信息。适用于查询新闻、科技资讯、天气预报、股票行情等需要实时数据的场景。"
)