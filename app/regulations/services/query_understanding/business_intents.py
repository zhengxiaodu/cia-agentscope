"""Rule-first business intent recognition and composable query rewrites.

This module deliberately does not execute retrieval or business decisions.  It
describes what the user needs and produces retrieval-oriented query variants
that a later planner may consume.
"""

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


BUSINESS_INTENTS: Tuple[str, ...] = (
    "fact",
    "eligibility",
    "process",
    "comparison",
    "temporal",
    "summary",
    "multi_hop",
    "exception",
    "calculation",
    "ambiguous",
    "exclusion",
    "citation",
)


_RULES: Dict[str, Sequence[str]] = {
    "fact": (
        r"第(?:几|[一二三四五六七八九十百千万零〇两\d]+)条(?:规定|内容|是什么|怎么说)",
        r"(?:补贴|报销|住宿|交通|费用|待遇).{0,8}(?:标准|定义)(?:是|为|多少|什么)",
        r"(?:补贴|报销|住宿|交通|费用|待遇).{0,8}(?:标准|定义)(?:呢)?[？?]?$",
        r"(?:什么是|如何定义|定义是什么)",
    ),
    "eligibility": (
        r"(?:符合|满足).{0,8}(?:条件|资格)",
        r"(?:申请|享受|领取).{0,6}(?:条件|资格)",
        r"(?:哪些人|什么人|谁)(?:可以|能|有资格).{0,8}(?:申请|享受|领取)",
        r"(?:哪些人|什么人|谁).{0,8}(?:不能|不可以|不得).{0,8}(?:申请|享受|领取)",
    ),
    "process": (
        r"(?:怎么|如何)(?:办理|申请|报销|操作|提交)",
        r"(?:办理|申请|审批|报销).{0,6}(?:流程|步骤|材料|手续)",
        r"(?:需要|准备|提交).{0,4}(?:哪些|什么)?材料",
        r"(?:流程|步骤|材料|手续)(?:是什么|有哪些|呢)?[？?]?$",
    ),
    "comparison": (
        r"(?:区别|差异|不同|对比|相比|有何不同|有什么区别|哪个更适合)",
        r"(?:新旧|新版.{0,5}旧版|旧版.{0,5}新版)",
    ),
    "temporal": (
        r"(?<!\d)(?:19|20)\d{2}年?(?!\d)",
        r"(?:什么时候|何时).{0,5}(?:生效|实施|发布|废止|失效)",
        r"(?:最新|现行|当前|历史版本|以前|过去|新版|旧版|有效期|生效日期)",
    ),
    "summary": (
        r"(?:总结|概括|摘要|核心内容|主要内容|简单介绍|简要介绍|整体介绍)",
    ),
    "multi_hop": (
        r"(?:结合|综合|根据).{0,30}(?:和|与|以及).{0,30}(?:规定|政策|办法|制度)",
        r"(?:跨|多个).{0,4}(?:政策|制度|文件|条款)",
    ),
    "exception": (
        r"(?:例外|除外|特殊情况|特殊情形|但书|原则上|例外规定|豁免)",
    ),
    "calculation": (
        r"(?:一共|总共|合计|最终).{0,8}(?:多少|多少钱|金额)",
        r"(?:能领|能报|可以领|可以报).{0,5}(?:多少|多少钱)",
        r"(?:按|按照).{0,8}(?:比例|标准|公式).{0,8}(?:计算|算|多少)",
        r"(?:每天|每月|每年|每人|每次)\s*\d+(?:\.\d+)?\s*(?:元|%|天).{0,20}\d+(?:\.\d+)?\s*(?:天|月|年|人|次)",
    ),
    "ambiguous": (
        r"^(?:这个|那个|该)?政策(?:怎么样|如何|能帮到我吗|有用吗)[？?]?$",
        r"^(?:这个|那个|该)(?:怎么办|怎么处理|可以吗)[？?]?$",
    ),
    "exclusion": (
        r"(?:哪些|什么|何种).{0,8}(?:不能|不可以|不得|禁止|不适用|排除)",
        r"(?:不能|不可以|不得|禁止|不适用|不允许).{0,12}(?:申请|报销|领取|享受|办理)",
        r"(?:禁止条款|排除情形|负面清单)",
    ),
    "citation": (
        r"(?:依据|根据).{0,6}(?:哪一条|哪条|第几条|什么条款|哪个文件)",
        r"(?:全文|原文|条款原文|出处|来源|引用|溯源)",
        r"(?:哪一条|哪条|第几条).{0,6}(?:规定|写的|提到)",
    ),
}


_RULE_CONFIDENCE = {
    "fact": 0.93,
    "eligibility": 0.95,
    "process": 0.94,
    "comparison": 0.96,
    "temporal": 0.93,
    "summary": 0.94,
    "multi_hop": 0.88,
    "exception": 0.96,
    "calculation": 0.95,
    "ambiguous": 0.96,
    "exclusion": 0.96,
    "citation": 0.97,
}


_LLM_PROMPT = """你是业务意图分类器。只从以下十二类中选择一个或多个：
fact, eligibility, process, comparison, temporal, summary, multi_hop,
exception, calculation, ambiguous, exclusion, citation。

这是业务意图分类，不是技术改写类型分类。请只返回 JSON：
{{"intents":["类型"],"confidence":{{"类型":0.0到1.0}}}}
不得输出枚举以外的类型，不得解释。

用户问题：{query}
"""


def _rule_matches(query: str) -> Dict[str, List[str]]:
    matches: Dict[str, List[str]] = {}
    for intent in BUSINESS_INTENTS:
        evidence = []
        for pattern in _RULES[intent]:
            match = re.search(pattern, query, flags=re.IGNORECASE)
            if match:
                evidence.append(match.group(0))
        if evidence:
            matches[intent] = list(dict.fromkeys(evidence))
    return matches


def _parse_llm_classification(raw: Any) -> Tuple[List[str], Dict[str, float]]:
    if not isinstance(raw, str) or not raw.strip():
        return [], {}
    candidate = raw.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", candidate,
            flags=re.IGNORECASE,
        ).strip()
    try:
        payload = json.loads(candidate)
    except (TypeError, json.JSONDecodeError):
        return [], {}
    if not isinstance(payload, dict) or not isinstance(payload.get("intents"), list):
        return [], {}
    intents = [
        value for value in payload["intents"]
        if isinstance(value, str) and value in BUSINESS_INTENTS
    ]
    intents = list(dict.fromkeys(intents))
    if not intents:
        return [], {}
    raw_confidence = payload.get("confidence", {})
    confidence: Dict[str, float] = {}
    for intent in intents:
        value = raw_confidence.get(intent, 0.75) if isinstance(raw_confidence, dict) else 0.75
        if isinstance(value, (int, float)):
            confidence[intent] = max(0.0, min(1.0, float(value)))
        else:
            confidence[intent] = 0.75
    return intents, confidence


async def analyze_business_intents(
    query: str,
    llm_call_fn: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Recognize multi-label business intent, using the LLM only if unclear."""
    query = (query or "").strip()
    matches = _rule_matches(query)
    intents = [intent for intent in BUSINESS_INTENTS if intent in matches]
    confidence = {intent: _RULE_CONFIDENCE[intent] for intent in intents}
    evidence = {intent: matches[intent] for intent in intents}
    sources = {intent: "rule" for intent in intents}
    llm_used = False

    # A strong explicit rule is conclusive. Multiple labels are expected, not
    # considered a conflict, because business scenarios compose naturally.
    if not intents and llm_call_fn is not None:
        llm_used = True
        try:
            raw = await llm_call_fn(_LLM_PROMPT.format(query=query))
        except Exception:
            raw = ""
        llm_intents, llm_confidence = _parse_llm_classification(raw)
        if llm_intents:
            intents = llm_intents
            confidence = llm_confidence
            evidence = {intent: [] for intent in intents}
            sources = {intent: "llm" for intent in intents}

    if not intents:
        intents = ["ambiguous"]
        confidence = {"ambiguous": 0.6}
        evidence = {"ambiguous": []}
        sources = {"ambiguous": "fallback"}

    return {
        "business_intents": intents,
        "intent_confidence": confidence,
        "intent_evidence": evidence,
        "intent_sources": sources,
        "llm_business_classification_used": llm_used,
    }


def extract_business_slots(query: str) -> Dict[str, Any]:
    """Extract only rewrite-relevant slots; deeper extraction belongs downstream."""
    years = re.findall(r"(?<!\d)((?:19|20)\d{2})年?(?!\d)", query)
    articles = re.findall(r"第[一二三四五六七八九十百千万零〇两\d]+条", query)
    quantities = [
        {"value": value, "unit": unit}
        for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(元|%|天|月|年|人|次)", query)
    ]
    return {
        "article_numbers": list(dict.fromkeys(articles)),
        "target_years": list(dict.fromkeys(years)),
        "quantities": quantities,
        "requested_citation": bool(re.search(r"原文|依据|哪一条|哪条|出处|来源", query)),
    }


_STRATEGY_NAMES = {
    "fact": "exact_fact",
    "eligibility": "eligibility_conditions",
    "process": "process_dimensions",
    "comparison": "comparison_dimensions",
    "temporal": "temporal_version",
    "summary": "summary_coverage",
    "multi_hop": "dependent_decomposition",
    "exception": "exception_pair",
    "calculation": "calculation_evidence",
    "ambiguous": "clarification_first",
    "exclusion": "negative_exclusion",
    "citation": "citation_locator",
}


_REWRITE_SUFFIXES = {
    "fact": "对应的具体条款、定义或数字标准",
    "eligibility": "申请资格、申请条件、全部必要条件、排除条件和判断口径",
    "process": "办理前置条件、所需材料、操作步骤、办理时限和负责部门",
    "comparison": "比较对象在适用条件、标准、流程和待遇方面的异同",
    "temporal": "适用版本、发布日期、生效日期、失效日期和历史规定",
    "summary": "政策目的、适用范围、核心要求、关键数字和重要例外",
    "multi_hop": "涉及的各项制度依据及其前后依赖关系",
    "exception": "通常规则以及例外、除外和特殊情形",
    "calculation": "计算公式、参数含义、适用条件、单位、封顶和保底规则",
    "exclusion": "禁止、不适用、不能办理和排除情形，以及对应例外",
    "citation": "准确的文档名称、章节、条款号和条款原文",
}


def build_business_rewrite_plan(
    query: str,
    business_intents: Sequence[str],
) -> Dict[str, Any]:
    """Build deterministic, composable rewrite variants for recognized intents."""
    intents = [intent for intent in business_intents if intent in BUSINESS_INTENTS]
    strategies = [_STRATEGY_NAMES[intent] for intent in intents]
    slots = extract_business_slots(query)

    if "ambiguous" in intents:
        return {
            "business_slots": slots,
            "rewrite_strategies": strategies,
            "business_rewrites": [],
            "clarification": {
                "required": True,
                "questions": ["请说明你想了解的具体政策，以及希望解决的问题是什么？"],
                "missing_slots": ["document", "user_goal"],
            },
        }

    rewrites = []
    clean_query = query.strip()
    if clean_query:
        rewrites.append(clean_query)
    for intent in intents:
        suffix = _REWRITE_SUFFIXES.get(intent)
        if suffix:
            rewrites.append(f"{clean_query}；检索{suffix}")

    return {
        "business_slots": slots,
        "rewrite_strategies": list(dict.fromkeys(strategies)),
        "business_rewrites": list(dict.fromkeys(rewrites)),
        "clarification": {
            "required": False,
            "questions": [],
            "missing_slots": [],
        },
    }
