from agentscope.tool import FunctionTool, ToolChunk

from app.config import (
    RAGFLOW_API_KEY,
    RAGFLOW_BASE_URL
)

DEFAULT_DATASET_IDS = []
DEFAULT_PAGE_SIZE = 30
DEFAULT_SIMILARITY_THRESHOLD = 0.2
DEFAULT_VECTOR_SIMILARITY_WEIGHT = 0.3
DEFAULT_TOP_K = 1024
DEFAULT_KEYWORD = False


async def ragflow_retrieval(
    question: str,
    dataset_ids: list = None,
    document_ids: list = None,
    page: int = 1,
    page_size: int = None,
    similarity_threshold: float = None,
    vector_similarity_weight: float = None,
    top_k: int = None,
    rerank_id: str = None,
    keyword: bool = None,
    cross_languages: list = None,
    metadata_condition: dict = None,
) -> ToolChunk:
    """使用 RAGFlow 从知识库中检索相关文档片段。

    Args:
        question: 用户的问题或查询关键词
        dataset_ids: 要检索的知识库ID列表，默认使用全局配置的 DEFAULT_DATASET_IDS
        document_ids: 要检索的文档ID列表，可选
        page: 分页页码，默认 1
        page_size: 每页返回的最大片段数，默认 30
        similarity_threshold: 最小相似度阈值，默认 0.2
        vector_similarity_weight: 向量余弦相似度权重，默认 0.3
        top_k: 参与向量计算的片段数，默认 1024
        rerank_id: 重排序模型 ID，可选
        keyword: 是否启用关键词匹配，默认 False
        cross_languages: 跨语言检索的目标语言列表，可选
        metadata_condition: 元数据过滤条件，可选
    """
    if not RAGFLOW_API_KEY:
        return ToolChunk(text="错误：未配置 RAGFLOW_API_KEY，请设置环境变量或在代码中配置 API Key。")

    if dataset_ids is None:
        dataset_ids = DEFAULT_DATASET_IDS
    if not dataset_ids:
        return ToolChunk(text="错误：未指定要检索的知识库（dataset_ids），请配置 DEFAULT_DATASET_IDS 或传入 dataset_ids 参数。")

    if page_size is None:
        page_size = DEFAULT_PAGE_SIZE
    if similarity_threshold is None:
        similarity_threshold = DEFAULT_SIMILARITY_THRESHOLD
    if vector_similarity_weight is None:
        vector_similarity_weight = DEFAULT_VECTOR_SIMILARITY_WEIGHT
    if top_k is None:
        top_k = DEFAULT_TOP_K
    if keyword is None:
        keyword = DEFAULT_KEYWORD

    try:
        from ragflow_sdk import RAGFlow

        rag_object = RAGFlow(api_key=RAGFLOW_API_KEY, base_url=RAGFLOW_BASE_URL)

        kwargs = {
            "question": question,
            "dataset_ids": dataset_ids,
            "page": page,
            "page_size": page_size,
            "similarity_threshold": similarity_threshold,
            "vector_similarity_weight": vector_similarity_weight,
            "top_k": top_k,
            "keyword": keyword,
        }

        if document_ids is not None:
            kwargs["document_ids"] = document_ids
        if rerank_id is not None:
            kwargs["rerank_id"] = rerank_id
        if cross_languages is not None:
            kwargs["cross_languages"] = cross_languages
        if metadata_condition is not None:
            kwargs["metadata_condition"] = metadata_condition

        chunks = rag_object.retrieve(**kwargs)

        if chunks:
            content_parts = []
            for i, chunk in enumerate(chunks):
                chunk_info = f"【{i + 1}】"
                if hasattr(chunk, "content") and chunk.content:
                    chunk_info += chunk.content
                elif isinstance(chunk, dict):
                    chunk_info += chunk.get("content", str(chunk))
                else:
                    chunk_info += str(chunk)

                metadata = []
                if hasattr(chunk, "document_name") and chunk.document_name:
                    metadata.append(f"文档: {chunk.document_name}")
                if hasattr(chunk, "similarity") and chunk.similarity is not None:
                    metadata.append(f"相似度: {chunk.similarity:.4f}")
                if hasattr(chunk, "id") and chunk.id:
                    metadata.append(f"ID: {chunk.id}")

                if metadata:
                    chunk_info += "\n" + " | ".join(metadata)

                content_parts.append(chunk_info)

            content = "\n\n".join(content_parts)
            content += f"\n\n共检索到 {len(chunks)} 个相关片段。"
        else:
            content = "未检索到相关文档片段。"

    except ImportError:
        content = "错误：未安装 ragflow-sdk 包，请先运行：pip install ragflow-sdk"
    except Exception as e:
        content = f"RAGFlow 检索异常: {str(e)}"

    return ToolChunk(text=content)


ragflow_retrieval_tool = FunctionTool(
    func=ragflow_retrieval,
    name="ragflow_retrieval",
    description="使用 RAGFlow 知识库检索功能，从指定知识库中检索与用户问题相关的文档片段。适用于查询内部知识库、企业文档、专业资料等场景。"
)
