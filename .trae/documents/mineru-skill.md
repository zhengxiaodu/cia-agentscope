# 新增 mineru 技能：解析 docx/doc/表格/图片/pdf 文件

## 一、需求摘要

新增一个 `mineru` 技能，用于解析用户上传的 docx / doc / 表格 / 图片 / pdf 格式文件，调用 MinerU API Gateway 异步解析接口，返回 Markdown 内容供智能体使用。

核心约束（来自用户）：
- **仿照卡片技能调用卡片工具的方式**：技能目录薄、主要代码放在 `/workspace/tools/` 下的工具文件里，技能通过 `tools.py` 引用工具。
- **MinerU URL 与 api-key 放在 `.env`**，通过 `app/config.py` 暴露给工具调用。
- **鉴权头是 `x-api-key`，不是 `Authorization`**。
- **绑定到 `general_agent`**（用户已确认），无需新建智能体、无需改 `intent_config.yml`。

## 二、现状分析（基于 Phase 1 探索）

### 2.1 既有可参照模式

| 模式 | 文件 | 特征 |
|------|------|------|
| 卡片技能（用户指定仿照） | [skills/card_interaction/tools.py](file:///workspace/skills/card_interaction/tools.py) + [tools/chart_tools.py](file:///workspace/tools/chart_tools.py) | 技能 `tools.py` 仅 `from tools.chart_tools import ...` 重导出；主代码在 `/workspace/tools/` 下；`SKILL.md` 带 `metadata.tools` + `metadata.source: skill-dir` |
| RAGFlow 检索（外部 API 异步范本） | [skills/ragflow_retrieval/ragflow_retrieval.py](file:///workspace/skills/ragflow_retrieval/ragflow_retrieval.py) | `async def` + `ToolChunk` 返回 + 模块级 `FunctionTool(...)` 包装 + `from app.config import RAGFLOW_API_KEY, RAGFLOW_BASE_URL`；未配置 key 时返回错误 `ToolChunk` |

→ 本方案采用**两者结合**：主代码放 `/workspace/tools/mineru_tools.py`（仿卡片），其中用 `async def` + `ToolChunk` + `FunctionTool`（仿 RAGFlow，因 MinerU 是异步轮询长任务）。

### 2.2 配置加载链路

- [app/config.py](file:///workspace/app/config.py) L4 `load_dotenv()`；L80-81 是 `RAGFLOW_API_KEY` / `RAGFLOW_BASE_URL` 的加载范式（`os.getenv(..., "")`）→ MinerU 在其后追加两行即可。
- [app/config.py](file:///workspace/app/config.py) L46-59 `UPLOAD_ALLOWED_MEDIA_TYPES`：当前包含 docx / pdf / 图片 / xls / xlsx / csv / ppt / pptx，**缺少 `application/msword`（.doc）** → 需补一项，否则 .doc 上传会被拦截，mineru 无法拿到文件。

### 2.3 技能/智能体注册链路

- [config/skill_config.yml](file:///workspace/config/skill_config.yml)：当前 4 个技能，新增 mineru 追加一条。
- [config/agent_config.yml](file:///workspace/config/agent_config.yml)：`general_agent.skills` 当前仅 `bocha_search`，追加 `mineru`；并更新其 `system_prompt` 提示"用户上传文档需解析时调用 mineru_parse"。
- [config/intent_config.yml](file:///workspace/config/intent_config.yml)：`general_chat` 意图已绑定 `general_agent`，**无需改动**。

### 2.4 上传文件落盘路径

[app/routes/upload.py](file:///workspace/app/routes/upload.py) 将文件保存到 `{WORKSPACE_BASEDIR}/{session_id}/data/{session_id}/{uuid}_{filename}`，`file_service.save_upload` 返回带 `file://` URI 的 DataBlock。→ mineru 工具的 `file_path` 入参需兼容 `file://` URI 与裸本地路径两种形态。

### 2.5 MinerU API 要点（来自用户手册）

- 提交：`POST {BASE_URL}/tasks`，`multipart/form-data`，字段 `file`（文件）+ `output_formats`（字符串，默认 `md`）；返回 201 含 `id`。
- 轮询：`GET {BASE_URL}/tasks/{task_id}/result`，看 `status`：`completed` 取 `result.md_content`；`failed` 抛错；否则 sleep 后重试。
- 鉴权：请求头 `x-api-key: <MINERU_API_KEY>`（**非 Authorization**）。
- 默认超时 300s、轮询间隔 2s（手册示例值，沿用）。

## 三、方案设计（决策完整，可直接执行）

### 3.1 新增文件

#### (A) `/workspace/tools/mineru_tools.py` —— 主代码（工具层）

职责：封装 MinerU 提交 + 轮询 + 返回 Markdown。要点：

```python
import asyncio
import logging
import os
from pathlib import Path

import httpx
from agentscope.tool import FunctionTool, ToolChunk

from app.config import MINERU_API_KEY, MINERU_BASE_URL

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_FORMATS = "md"
DEFAULT_TIMEOUT = 300        # 秒，沿用手册示例
DEFAULT_POLL_INTERVAL = 2    # 秒

# MinerU 支持的文件扩展名（仅用于入参提示与日志，实际校验由 MinerU magic bytes 完成）
SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx",
    ".xls", ".xlsx", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
}


def _normalize_file_path(file_path: str) -> str:
    """兼容 file:// URI 与裸本地路径，返回本地路径。"""
    if not file_path:
        raise ValueError("file_path 不能为空")
    if file_path.startswith("file://"):
        file_path = file_path[len("file://"):]
    return file_path


async def mineru_parse(
    file_path: str,
    output_formats: str = DEFAULT_OUTPUT_FORMATS,
    timeout: int = DEFAULT_TIMEOUT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
) -> ToolChunk:
    """调用 MinerU 解析本地文档（docx/doc/表格/图片/pdf），返回 Markdown 内容。

    Args:
        file_path: 本地文件路径或 file:// URI（来自上传组件）
        output_formats: 输出格式，逗号分隔（md / content_list / middle_json / model_output / images），默认 md
        timeout: 轮询最大等待秒数，默认 300
        poll_interval: 轮询间隔秒数，默认 2
    """
    # 1. 配置校验
    if not MINERU_API_KEY:
        return ToolChunk(text="错误：未配置 MINERU_API_KEY，请在 .env 中设置。")
    if not MINERU_BASE_URL:
        return ToolChunk(text="错误：未配置 MINERU_BASE_URL，请在 .env 中设置。")

    headers = {"x-api-key": MINERU_API_KEY}  # 关键：x-api-key，非 Authorization
    base = MINERU_BASE_URL.rstrip("/")

    # 2. 路径归一化与存在性校验
    try:
        local_path = _normalize_file_path(file_path)
    except ValueError as e:
        return ToolChunk(text=f"错误：{e}")
    if not os.path.isfile(local_path):
        return ToolChunk(text=f"错误：文件不存在：{local_path}")

    ext = Path(local_path).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        logger.warning(f"[mineru] 扩展名 {ext} 不在推荐列表，仍尝试提交（由 MinerU 校验）")

    try:
        # 3. 提交任务（multipart/form-data）
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(local_path, "rb") as f:
                resp = await client.post(
                    f"{base}/tasks",
                    headers=headers,
                    files={"file": (os.path.basename(local_path), f)},
                    data={"output_formats": output_formats},
                )
            if resp.status_code != 201:
                return ToolChunk(
                    text=f"MinerU 提交失败: HTTP {resp.status_code} - {resp.text}"
                )
            task_id = resp.json().get("id")
            if not task_id:
                return ToolChunk(text=f"MinerU 提交响应缺少 task id: {resp.text}")
            logger.info(f"[mineru] 任务已提交: {task_id} ({os.path.basename(local_path)})")

        # 4. 轮询结果
        elapsed = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            while elapsed < timeout:
                r = await client.get(
                    f"{base}/tasks/{task_id}/result",
                    headers=headers,
                )
                if r.status_code != 200:
                    return ToolChunk(
                        text=f"MinerU 轮询失败: HTTP {r.status_code} - {r.text}"
                    )
                data = r.json()
                status = data.get("status")
                if status == "completed":
                    result = data.get("result") or {}
                    md = result.get("md_content")
                    if not md:
                        # 兜底：result 为空时回传原始结构摘要
                        return ToolChunk(
                            text=f"MinerU 解析完成但 md_content 为空，原始结果: {data}"
                        )
                    return ToolChunk(text=md)
                if status == "failed":
                    err = data.get("error_message", "未知错误")
                    return ToolChunk(text=f"MinerU 任务失败: {err}")
                # pending / running → 继续等
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

        return ToolChunk(
            text=f"MinerU 任务超时：{task_id} 在 {timeout}s 内未完成。"
        )

    except httpx.HTTPError as e:
        logger.exception("[mineru] HTTP 请求异常")
        return ToolChunk(text=f"MinerU 网络异常: {e}")
    except Exception as e:
        logger.exception("[mineru] 解析异常")
        return ToolChunk(text=f"MinerU 解析异常: {e}")


# 模块级 FunctionTool 包装（同 ragflow_retrieval 范式）
mineru_parse_tool = FunctionTool(
    func=mineru_parse,
    name="mineru_parse",
    description=(
        "调用 MinerU 解析用户上传的文档（支持 pdf / docx / doc / xls / xlsx / csv / 图片），"
        "返回解析后的 Markdown 文本。适用于用户上传文件并要求提取/总结/问答文档内容的场景。"
    ),
)
```

说明：
- 用 `httpx.AsyncClient`（项目已依赖，见 [tools/card_config_tools.py](file:///workspace/tools/card_config_tools.py) L15、L87）。提交与轮询分两个 client，避免长轮询占用提交连接。
- 所有错误路径都返回 `ToolChunk(text=...)`（不抛异常打断编排），与 ragflow 一致。
- `files={"file": (filename, f)}` 显式带文件名，满足 MinerU "Filename is required" 要求。

#### (B) `/workspace/skills/mineru/tools.py` —— 技能引用工具（薄封装）

仿 [skills/card_interaction/tools.py](file:///workspace/skills/card_interaction/tools.py)：

```python
from tools.mineru_tools import (
    mineru_parse,
    mineru_parse_tool,
)
```

#### (C) `/workspace/skills/mineru/SKILL.md` —— 技能文档

YAML front matter 仿 card_interaction（`metadata.tools` + `metadata.source: skill-dir`），正文说明适用场景、入参、示例。要点：

```yaml
---
name: mineru
description: |
  MinerU 文档解析技能，将用户上传的 pdf/docx/doc/表格/图片解析为 Markdown。
  Use when: 用户上传了文件并要求提取内容、总结、翻译、问答文档内容。
license: MIT
metadata:
  source: skill-dir
  tools:
    - mineru_parse
---
```

正文包含：功能说明、配置说明（`MINERU_API_KEY` / `MINERU_BASE_URL` 环境变量表）、`mineru_parse` 入参表（file_path 必填、output_formats/timeout/poll_interval 可选）、调用示例（用户上传 pdf 后调用 `mineru_parse(file_path=...)`）、注意事项（鉴权用 x-api-key、文件需先经 /upload 落盘、超时默认 300s）。

### 3.2 修改文件

#### (D) `/workspace/app/config.py`

1. L59 后（`UPLOAD_ALLOWED_MEDIA_TYPES` 列表内）追加一项支持 .doc：
   ```python
   "application/msword",  # doc
   ```
   （插在 docx 那一行之后，保持 office 系列分组一致）

2. L81 后追加 MinerU 配置（紧接 RAGFLOW 两行）：
   ```python
   MINERU_API_KEY = os.getenv("MINERU_API_KEY", "")
   MINERU_BASE_URL = os.getenv("MINERU_BASE_URL", "")
   ```

#### (E) `/workspace/.env.example`

在文件末尾（RAGFLOW 段之后）追加：

```env
# MinerU 文档解析配置（鉴权头为 x-api-key）
MINERU_API_KEY=
MINERU_BASE_URL=
```

#### (F) `/workspace/.env`

追加同样的两行（值为空占位，由用户填入真实凭据）。若 `.env` 已含敏感值则仅追加这两行，不动其余。

#### (G) `/workspace/config/skill_config.yml`

在 `skills` 列表末尾追加：

```yaml
  - name: mineru
    directory: ../skills/mineru
    description: "MinerU 文档解析技能，将用户上传的 pdf/docx/doc/表格/图片解析为 Markdown，适用于提取、总结、问答文档内容。"
```

#### (H) `/workspace/config/agent_config.yml`

1. `general_agent.skills` 追加 `- mineru`：
   ```yaml
  - id: general_agent
    name: 通用问答智能体
    skills:
      - bocha_search
      - mineru
   ```
2. `general_agent.system_prompt` 追加文档解析指引（在现有 prompt 末尾续写，不改动原有搜索/常识两段）：
   ```text
   - 如果用户上传了文件（pdf/docx/doc/表格/图片）并要求提取内容、总结、翻译或基于文档问答，调用 mineru_parse 工具解析文件得到 Markdown，再据此回答；文件路径来自上传组件返回的 file:// URI 或本地路径
   ```

## 四、假设与决策

1. **绑定 general_agent**（用户确认），不改 intent_config.yml。
2. **主代码放 `/workspace/tools/mineru_tools.py`**（用户"主要代码放在工具里"），技能 `tools.py` 仅重导出（仿 card_interaction）。
3. **异步实现**：MinerU 是长任务轮询，用 `async def` + `httpx.AsyncClient` + `ToolChunk`（仿 ragflow_retrieval），避免阻塞编排事件流。
4. **错误不抛异常**：所有失败路径返回带错误信息的 `ToolChunk`，与 ragflow 一致，保证编排流不中断。
5. **file_path 兼容 `file://` URI**：因 upload 组件返回 `file://` URI。
6. **超时/轮询沿用手册默认值**：300s / 2s，作为函数默认参数可被调用方覆盖。
7. **.doc 上传支持**：补 `application/msword` 到 `UPLOAD_ALLOWED_MEDIA_TYPES`，否则 .doc 在 upload 层就被拒，mineru 拿不到文件。
8. **`.env` 仅追加占位**：真实 MINERU_API_KEY / MINERU_BASE_URL 由用户填入；提交代码不含密钥。
9. **不改 intent_config.yml**：general_chat 已兜底到 general_agent。
10. **依赖 httpx**：项目已使用（card_config_tools.py），无需新增依赖。

## 五、验证步骤

1. **配置加载**：启动 app，确认日志无 ImportError；`from app.config import MINERU_API_KEY, MINERU_BASE_URL` 可正常导入。
2. **技能注册**：调用 `/chat` 走 general_chat 意图，确认 general_agent 工具列表包含 `mineru_parse`（可通过日志或 agent registry 输出验证）。
3. **.doc 上传**：用 .doc 文件调 `/upload`，确认不再被 `UPLOAD_ALLOWED_MEDIA_TYPES` 拒绝（之前会 400）。
4. **端到端解析**：
   - `.env` 填入真实 `MINERU_API_KEY` / `MINERU_BASE_URL`
   - 上传一个 pdf → 在 `/chat` 中问"总结这个文件" → 确认 general_agent 调用 `mineru_parse`，最终回复包含解析出的 Markdown 摘要
5. **未配置降级**：临时清空 `MINERU_API_KEY`，调用工具，确认返回 `ToolChunk(text="错误：未配置 MINERU_API_KEY...")` 而非抛异常。
6. **超时/失败降级**：构造一个无法解析的文件或断开 BASE_URL，确认返回友好错误信息而非 500。
7. **提交**：`git add` 上述新增/修改文件 → commit → push 到 `origin/trae/agent-5CYjia`。

## 六、执行顺序（实现阶段）

1. 编辑 [app/config.py](file:///workspace/app/config.py)（+2 配置项、+1 msword）
2. 编辑 [.env.example](file:///workspace/.env.example) 与 [.env](file:///workspace/.env)（+MinerU 段）
3. 新建 [tools/mineru_tools.py](file:///workspace/tools/mineru_tools.py)（主代码）
4. 新建 [skills/mineru/tools.py](file:///workspace/skills/mineru/tools.py)（重导出）
5. 新建 [skills/mineru/SKILL.md](file:///workspace/skills/mineru/SKILL.md)（技能文档）
6. 编辑 [config/skill_config.yml](file:///workspace/config/skill_config.yml)（+mineru 条目）
7. 编辑 [config/agent_config.yml](file:///workspace/config/agent_config.yml)（general_agent +skill +prompt）
8. `git add` → commit → push `origin/trae/agent-5CYjia`
