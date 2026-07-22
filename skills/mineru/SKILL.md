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

# MinerU 文档解析技能

## 功能说明

本技能通过 MinerU API Gateway 异步解析用户上传的文档，返回 Markdown 文本，供智能体据此回答用户问题。主代码位于 `tools/mineru_tools.py`，本目录的 `tools.py` 仅作重导出引用（与 `card_interaction` 技能结构一致）。

支持的文件格式：
- 文档：pdf / docx / doc
- 表格：xls / xlsx / csv
- 图片：png / jpg / jpeg / gif / webp / bmp

## 配置说明

### 环境变量（写入 `.env`，经 `app/config.py` 暴露）

| 环境变量 | 说明 | 示例 |
|----------|------|------|
| `MINERU_API_KEY` | MinerU API 密钥 | `xxxxxxxxxx` |
| `MINERU_BASE_URL` | MinerU 服务地址 | `http://localhost:8000` |

> ⚠️ 鉴权头为 `x-api-key`，**不是** `Authorization`。该细节已在工具代码内固化，无需调用方关心。

### 默认参数（在 `tools/mineru_tools.py` 顶部，可在调用时覆盖）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DEFAULT_OUTPUT_FORMATS` | `md` | 默认输出格式 |
| `DEFAULT_TIMEOUT` | `300` | 轮询最大等待秒数 |
| `DEFAULT_POLL_INTERVAL` | `2` | 轮询间隔秒数 |

## 工具使用

### 工具名称

`mineru_parse`

### 入参格式

```json
{
    "file_path": "file:///data/docker-workspaces/xxx/xxx.pdf",
    "output_formats": "md"
}
```

### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file_path | string | 是 | 本地文件路径或 `file://` URI（来自上传组件返回） |
| output_formats | string | 否 | 输出格式，逗号分隔（md / content_list / content_list_v2 / middle_json / model_output / images），默认 md |
| timeout | int | 否 | 轮询最大等待秒数，默认 300 |
| poll_interval | int | 否 | 轮询间隔秒数，默认 2 |

### 输出格式

返回 `ToolChunk`，其 `text` 为解析后的 Markdown 文本。失败时返回带"错误："前缀的说明文本，不抛异常。

## 使用示例

### 示例 1：解析上传的 pdf

**用户输入**: 上传一个 `report.pdf` 后问"总结这个文件"

**调用工具**: `mineru_parse`

**工具参数**:
```json
{
    "file_path": "file:///data/docker-workspaces/sess-123/data/sess-123/abc_report.pdf"
}
```

### 示例 2：同时取 Markdown 与内容列表

**工具参数**:
```json
{
    "file_path": "/data/docker-workspaces/sess-123/data/sess-123/abc_report.pdf",
    "output_formats": "md,content_list"
}
```

## 注意事项

1. **先上传再解析**：文件需先经 `/upload` 接口落盘，拿到 `file://` URI 或本地路径后再传入 `file_path`。
2. **鉴权用 x-api-key**：由工具内部处理，调用方无需关心；密钥放 `.env`，不要硬编码到代码。
3. **超时默认 300s**：大文件/多页 PDF 解析较慢，必要时可显式传更大的 `timeout`。
4. **错误不抛异常**：所有失败路径（未配置、文件不存在、提交失败、轮询失败、超时）均返回带错误信息的 `ToolChunk`，不会打断编排流程。
5. **依赖 httpx**：项目已依赖，无需额外安装。
