---
name: ragflow_retrieval
description: 使用 RAGFlow 知识库检索功能，从指定知识库中检索与用户问题相关的文档片段。适用于查询内部知识库、企业文档、专业资料等场景。
license: Apache-2.0
metadata:
  author: AI Assistant
  version: "1.0"
  tags:
    - ragflow
    - retrieval
    - knowledge-base
    - rag
---

# RAGFlow 知识库检索技能

## 功能说明

本技能通过 RAGFlow 的 Python SDK 调用知识库检索功能，根据用户问题从指定知识库中检索相关的文档片段（chunks）。

## 配置说明

### 1. 安装依赖

```bash
pip install ragflow-sdk
```

### 2. 配置方式

检索配置分为两类，放置位置不同：

#### 敏感配置（API Key、Base URL）

优先使用**环境变量**配置，避免密钥硬编码：

| 环境变量 | 说明 | 示例 |
|----------|------|------|
| `RAGFLOW_API_KEY` | RAGFlow API 密钥 | `sk-xxxxxxxxxx` |
| `RAGFLOW_BASE_URL` | RAGFlow 服务地址 | `http://localhost:9380` |

也可直接在 `ragflow_retrieval.py` 文件顶部的常量区修改：

```python
RAGFLOW_API_KEY = "your-api-key-here"
RAGFLOW_BASE_URL = "http://localhost:9380"
```

#### 检索参数配置

默认检索参数在 `ragflow_retrieval.py` 文件顶部配置，调用工具时也可动态传入覆盖默认值：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DEFAULT_DATASET_IDS` | `[]` | 默认检索的知识库ID列表 |
| `DEFAULT_PAGE_SIZE` | `30` | 默认返回的最大片段数 |
| `DEFAULT_SIMILARITY_THRESHOLD` | `0.2` | 默认相似度阈值 |
| `DEFAULT_VECTOR_SIMILARITY_WEIGHT` | `0.3` | 向量相似度权重（关键词相似度权重为 1 - 该值） |
| `DEFAULT_TOP_K` | `1024` | 参与向量计算的片段数 |
| `DEFAULT_KEYWORD` | `False` | 是否默认启用关键词匹配 |

## 工具使用

### 工具名称

`ragflow_retrieval`

### 入参格式

```json
{
    "question": "用户的问题",
    "dataset_ids": ["kb_id_1", "kb_id_2"],
    "page_size": 10,
    "similarity_threshold": 0.3
}
```

### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| question | string | 是 | 用户的问题或查询关键词 |
| dataset_ids | list[string] | 否 | 要检索的知识库ID列表，未传则使用默认配置 |
| document_ids | list[string] | 否 | 要检索的文档ID列表，需确保所有文档使用同一 embedding 模型 |
| page | int | 否 | 分页页码，默认 1 |
| page_size | int | 否 | 每页返回的最大片段数，默认 30 |
| similarity_threshold | float | 否 | 最小相似度阈值，默认 0.2 |
| vector_similarity_weight | float | 否 | 向量余弦相似度权重，默认 0.3 |
| top_k | int | 否 | 参与向量计算的片段数，默认 1024 |
| rerank_id | string | 否 | 重排序模型 ID |
| keyword | boolean | 否 | 是否启用关键词匹配，默认 false |
| cross_languages | list[string] | 否 | 跨语言检索的目标语言列表 |
| metadata_condition | object | 否 | 元数据过滤条件 |

### 输出格式

返回检索到的文档片段列表，每个片段包含内容、文档名称、相似度等信息。

## 使用示例

### 示例 1：简单检索

**用户输入**: "什么是 RAGFlow？"

**调用工具**: `ragflow_retrieval`

**工具参数**:
```json
{
    "question": "什么是 RAGFlow？"
}
```

### 示例 2：指定知识库检索

**用户输入**: "查询产品手册中的安装步骤"

**调用工具**: `ragflow_retrieval`

**工具参数**:
```json
{
    "question": "安装步骤",
    "dataset_ids": ["product_manual_kb_id"],
    "page_size": 5,
    "similarity_threshold": 0.5
}
```

### 示例 3：混合检索（向量 + 关键词）

**用户输入**: "查找关于性能优化的文档"

**调用工具**: `ragflow_retrieval`

**工具参数**:
```json
{
    "question": "性能优化",
    "dataset_ids": ["tech_docs_kb_id"],
    "keyword": true,
    "vector_similarity_weight": 0.5
}
```

## 注意事项

1. **API Key 安全**: 建议使用环境变量配置 `RAGFLOW_API_KEY`，不要将密钥直接提交到代码仓库
2. **知识库 ID**: 使用前需确认 `dataset_ids` 正确，可通过 RAGFlow 管理界面或 `list_datasets` API 获取
3. **Embedding 模型一致**: 当使用 `document_ids` 过滤时，需确保所有文档使用相同的 embedding 模型，否则会报错
4. **依赖安装**: 使用前需安装 `ragflow-sdk` 包
5. **结果解析**: 返回结果为文本片段列表，需结合用户问题整理成自然语言回答
