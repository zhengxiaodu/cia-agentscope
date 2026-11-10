# 计划：SSE 流式接口加服务端心跳

## Summary
在 `/chat` SSE 流式响应中加入服务端心跳：源流超过 N 秒（默认 15s）无事件时自动发送 SSE 注释行 `: ping\n\n`，防止工具执行/mineru 解析等待等长静默期被反向代理按空闲连接切断（即"会话被用户中断"误报的根因）。问题 2（png 404）按用户要求不改。

## Current State Analysis
- [app/routes/chat.py](app/routes/chat.py) 的 `stream()` 直接透传 `generate_response` 事件；静默期（编排层工具执行、上传解析轮询）无任何字节下发，代理（nginx 默认 60s / 云 LB 60s idle）切断连接 → uvicorn cancel scope 取消 → [chat_service.py:751](app/services/chat_service.py#L751) 捕获 CancelledError 误报"用户中断"
- 配置模式：[app/config.py](app/config.py) 全部 `os.getenv` 模块级常量；`.env.example` 带注释条目
- 前端解析：事件均为 `data: {...}\n\n` 格式；SSE 规范中 `:` 开头的注释行会被 EventSource 及所有 `data:` 行解析器忽略，心跳对前端无感

## 核心设计：为什么用 queue + pump 包装器

**不能用** `asyncio.wait_for(anext(gen), timeout)`：超时会 cancel 掉 `__anext__` 协程，CancelledError 直接打进 `generate_response` 当前挂起点（如 LLM 调用），破坏主流程——等于每次心跳都"中断"一次会话。

**正确做法**：独立 pump task 持续从源生成器拉事件放入 `asyncio.Queue`；消费循环对 `queue.get()` 设超时，超时即 yield 心跳行。源生成器永远不被心跳超时触碰。

```
generate_response ──(pump task, 不受心跳影响)──> asyncio.Queue ──(wait_for 超时→心跳)──> SSE 响应
```

## Proposed Changes

### 1. app/config.py — 新增心跳间隔配置
```python
# SSE 流式响应心跳间隔（秒）：源流静默超过该时长时发送 ': ping' 注释行，
# 防止反向代理按空闲连接切断；<=0 禁用心跳
SSE_HEARTBEAT_INTERVAL = float(os.getenv("SSE_HEARTBEAT_INTERVAL", "15"))
```

### 2. .env.example — 新增条目
```ini
# SSE 流式响应心跳间隔（秒），防代理空闲超时切断；<=0 禁用
SSE_HEARTBEAT_INTERVAL=15
```

### 3. app/routes/chat.py — 心跳包装器 + 接入 stream()

新增模块级常量与辅助函数：
```python
from app.config import SSE_HEARTBEAT_INTERVAL

_SSE_HEARTBEAT = ": ping\n\n"   # SSE 注释行：EventSource/data: 解析器均忽略
_DONE = object()                 # 队列结束哨兵


async def _with_heartbeat(source):
    """SSE 心跳包装器：源流超过 SSE_HEARTBEAT_INTERVAL 秒无事件时发送 ': ping'。

    用 queue + pump 而非 wait_for(anext)：后者超时会取消底层生成器的当前 await，
    等于心跳本身"中断"会话。pump task 全程独立运行，仅在消费方退出（正常结束/
    客户端断开）时被 cancel——取消语义与现状一致（CancelledError 照常传入
    generate_response 的 751 处理分支，落库/trace 收尾逻辑不变）。
    源流抛异常时经队列透传给消费方，避免消费方无限心跳。
    SSE_HEARTBEAT_INTERVAL <= 0 时直接透传源流。
    """
    if SSE_HEARTBEAT_INTERVAL <= 0:
        async for ev in source:
            yield ev
        return

    queue: asyncio.Queue = asyncio.Queue()

    async def pump():
        try:
            async for ev in source:
                await queue.put(ev)
        except Exception as e:          # 源流异常 → 透传（否则消费方会无限心跳）
            await queue.put(e)
            return
        await queue.put(_DONE)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=SSE_HEARTBEAT_INTERVAL
                )
            except asyncio.TimeoutError:
                yield _SSE_HEARTBEAT
                continue
            if item is _DONE:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass
```

`stream()` 内接入（仅包一层，其余不动）：
```python
async for event in _with_heartbeat(generate_response(...)):
    yield event
```

## Assumptions & Decisions
- 心跳格式用 SSE 注释行 `: ping\n\n`：符合 SSE 规范，EventSource/fetch-event-source/自研 `data:` 解析器均忽略，前端零改动
- 默认 15s：对 nginx 默认 60s 读超时留 4 倍余量；可用环境变量调整或置 0 禁用
- 异常不吞：源流异常照常抛给 Starlette（与现状一致），仅多一层队列透传
- 客户端断开时行为不变：Starlette cancel `_with_heartbeat` → finally cancel pump → CancelledError 传入 generate_response → 751 分支落库收尾，与改造前完全一致
- 不动 chat_service.py、不动编排层

## Verification（按用户要求最小化）
1. `python -m py_compile app/config.py app/routes/chat.py`
2. 不跑测试（现有 test_chat_complete_generation.py 等在 TestClient 下事件即时产生，心跳不会触发，行为不变；用户明确要求少做/不做测试）
