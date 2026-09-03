# ============================================================
# 内部政策问答 - Query 理解与改写完整模块
# 包含：规则预过滤、实体提取、会话记忆、决策树改写
# ============================================================

import json
import os as _os
import re
import time
from typing import List, Dict, Optional, Any, Callable
from .business_intents import analyze_business_intents, build_business_rewrite_plan
from .memory import SessionMemory


# ============================================================
# 1. 词典与配置（专用）
# ============================================================

PRONOUNS = {
    "它", "这个", "那个", "这些", "那些", "刚才", "上面", "前面", "之前",
    "上一个", "上文", "刚才说的", "刚刚提到的", "那它", "那这个", "那份",
    "这份", "其", "该", "上述", "前述", "本制度", "本办法"
}

QUESTION_PARTICLES = {"呢", "吗", "怎么样", "如何", "多少", "啥", "嘛", "吧", "啊"}

PARALLEL_CONNECTORS = {
    "并且", "还有", "另外", "同时", "以及", "和", "与", "或者", "还是",
    "再问", "顺便", "另外问", "还有个"
}

COLLOQUIAL_WORDS = {
    "咋", "咋办", "整", "搞", "弄", "那个啥", "怎么说呢",
    "有没有", "能不能", "行不行", "好不好办", "麻烦不",
    "费不费事", "容易吗", "难不难", "给批吗", "过不过",
    "能报吗", "能领吗", "能申请吗", "走流程吗", "走审批吗",
    "找谁", "找哪个部门", "找谁签字"
}

TONE_PARTICLES = {"啊", "呀", "呢", "吧", "嘛", "哦", "哈", "啦", "咯", "呗"}

TROUBLESHOOTING_WORDS = {
    "为什么", "怎么回事", "怎么解决", "怎么办", "被拒了", "不给办",
    "不符合", "不够条件", "材料不全", "缺材料", "少材料",
    "审核不过", "没通过", "被退回", "卡住了", "办不了",
    "报不了", "领不到", "申请不了", "用不了", "登不上",
    "权限不够", "没权限", "系统提示", "报错", "失败了",
    "过期了", "失效了", "取消了", "停了", "改了", "更新了"
}

OPEN_WORDS = {
    "有哪些", "有什么", "最新", "目前", "现在", "全面", "详细",
    "都有哪些", "包括哪些", "覆盖哪些", "适用哪些",
    "怎么选", "如何选择", "对比一下", "区别是什么",
    "制度汇总", "政策清单", "相关制度", "相关规定", "操作指引"
}

PHENOMENON_WORDS = {
    "报不了", "报销不了", "请不了假", "休不了", "打不上卡",
    "权限没有", "登不上", "进不去", "看不到", "下载不了",
    "审批卡住", "一直待审批", "被退回", "材料被退",
    "额度不够", "超标了", "不符合标准",
    "登记失败", "托管失败", "结算失败", "清算失败",
    "账户开不了", "变更不了", "注销不了",
    "指令发不出去", "匹配不上", "对不上", "对账不平",
    "披露不了", "报送失败", "校验不通过"
}

NEGATION_WORDS = {
    "不", "不能", "不可以", "禁止", "不得", "无法", "没", "没有",
    "勿", "不可", "不允许", "不准",
}

# "别" 单独处理：作为独立词才视为否定（避免 "分别""识别" 误伤）
_SINGLE_CHAR_NEGATION = {"别"}


COMPARISON_WORDS = {
    "区别", "不同", "差异", "对比", "相比", "哪个更好", "还是",
    "差别", "不一样", "有何不同", "有什么区别"
}

TIME_WORDS = {
    "最新", "目前", "现在", "今年", "去年", "明年", "最近", "近期",
    "新版", "现行", "2024", "2025", "2026", "本年度", "上一年度"
}

SUGGESTION_WORDS = {
    "如何优化", "怎么改进", "如何完善", "怎样提升", "优化建议",
    "改进建议", "如何改善", "怎么提升", "如何调整", "改进方案",
    "如何升级", "如何改革", "完善建议", "优化方案"
}

FULL_DOC_WORDS = {
    "全文", "原文", "完整版", "完整内容", "整份", "整个文件",
    "全部内容", "原始文件", "完整制度", "完整规定", "完整办法",
    "发我全文", "给我原文", "看完整的", "完整文档"
}

# 业务实体正则
SPECIFIC_ENTITY_PATTERNS = [
    r"[A-Za-z]+[-_]?\d+",
    r"\d{4}年\d{1,2}月",
    r"第?\d+号",
    r"中债字〔?\d{4}〕?\d+号",
    r"制度|办法|规定|细则|指引|规范|手册|流程|通知|公告|意见",
    r"考勤|请假|加班|调休|年假|事假|病假|婚假|产假|陪产假",
    r"报销|差旅|交通|餐饮|住宿|发票|预算|费用",
    r"入职|转正|调岗|离职|劳动合同|竞业|保密",
    r"绩效|考核|晋升|调薪|奖金|福利|社保|公积金|补充医疗",
    r"信息安全|数据安全|权限申请|账号开通|VPN|堡垒机|邮件",
    r"债券登记|债券托管|债券结算|债券清算|中央结算",
    r"账户|债券账户|资金账户|保管箱|名义持有人",
    r"发行|分销|交易|结算|交割|过户|冻结|解冻|质押|解押",
    r"信息披露|估值|付息|兑付|赎回|回售|提前偿还",
    r"中债登|中债估值|中债黄皮书|中债指数|中债SRD",
    r"DVP|FOP|RTGS|净额结算|全额结算|双边净额",
    r"操作指引|业务指南|接口规范|报文|直联|客户端",
    r"登记结算|托管部|结算部|清算部|账户部|客服|运营|风控|合规|科技|人力|财务|办公",
    r"中债系统|登记系统|结算系统|清算系统|披露系统|客户端|网银"
]

# 制度别名映射（可继续扩展）
POLICY_ALIAS = {
    "差旅制度": "差旅费管理办法",
    "差旅费": "差旅费管理办法",
    "报销制度": "费用报销管理办法",
    "考勤制度": "考勤管理办法",
    "年假": "职工带薪年休假实施办法",
    "事假": "员工事假管理办法",
    "福利制度": "员工福利管理办法",
    "福利": "员工福利管理办法",
}

# 从外部 JSON 扩展的业务术语
_EXTRA_BUSINESS_TERMS: List[str] = []

# 运行时从外部 JSON 加载配置（覆盖默认值）
_CONFIG_PATH = _os.path.join(_os.path.dirname(__file__), "entity_config.json")


def load_entity_config(config_path: str = _CONFIG_PATH) -> dict:
    """从 JSON 文件加载实体配置，合并到全局词表。不存在或格式错误时静默跳过。"""
    if not _os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

    # 合并到全局变量
    if "policy_alias" in cfg:
        POLICY_ALIAS.update(cfg["policy_alias"])
    for key in ("pronouns", "parallel_connectors", "colloquial_words",
                "troubleshooting_words", "negation_words", "comparison_words",
                "time_words", "full_doc_words", "open_words", "phenomenon_words",
                "suggestion_words"):
        if key in cfg:
            target = globals().get(key.upper())
            if isinstance(target, set):
                target.update(cfg[key])
    # business_terms 追加到全局扩展列表
    if "business_terms" in cfg:
        _EXTRA_BUSINESS_TERMS.extend(cfg["business_terms"])
    return cfg


# 模块导入时加载项目配置，使 entity_config.json 中的新词及别名立即生效。
load_entity_config()


# ============================================================
# 3. 工具函数
# ============================================================

def normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[，。！？、；：""''（）【】]", " ", text)
    return text


def has_any(text: str, words: set) -> bool:
    return any(w in text for w in words)


def has_pronoun(query: str) -> bool:
    q = normalize(query)
    if has_any(q, {word for word in PRONOUNS if len(word) > 1}):
        return True
    if re.search(r"(?<!其)它", q):
        return True
    # 单字代词不能做普通子串匹配，否则“其他”、“应该”会被当成追问。
    if re.search(r"其(?!他|它|次|中|实|余|本|后|前)", q):
        return True
    return bool(re.search(
        r"(?<!应)该(?=制度|办法|规定|政策|流程|文件|通知|指引|细则|"
        r"业务|申请|报销|审批|结算|如何|怎么|是否|能否|可否|有|没|不|需|要|由|对|在|为|项|类|笔|份|条|款)",
        q,
    ))


def ends_with_question_particle(query: str) -> bool:
    q = normalize(query)
    return any(q.endswith(p) for p in QUESTION_PARTICLES)


def count_question_marks(query: str) -> int:
    return query.count("？") + query.count("?")


def has_parallel_connector(query: str) -> bool:
    q = normalize(query)
    if has_any(q, {word for word in PARALLEL_CONNECTORS if len(word) > 1}):
        return True
    # “和/与”只在两侧都有内容时视为连接词，并排除常见固定词内部。
    if re.search(r"(?<=[\w\u4e00-\u9fff])(?<!共)和(?!平|谐|尚|睦|气|解)(?=[\w\u4e00-\u9fff])", q):
        return True
    return bool(re.search(r"(?<=[\w\u4e00-\u9fff])(?<!参)与(?!其)(?=[\w\u4e00-\u9fff])", q))


def has_colloquial_words(query: str) -> bool:
    return has_any(normalize(query), COLLOQUIAL_WORDS)


def has_tone_particles(query: str) -> bool:
    q = normalize(query)
    return any(q.endswith(p) or f" {p}" in q for p in TONE_PARTICLES)


def has_specific_entity(query: str) -> bool:
    for pattern in SPECIFIC_ENTITY_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False


def has_troubleshooting_words(query: str) -> bool:
    return has_any(normalize(query), TROUBLESHOOTING_WORDS)


def starts_with_open_words(query: str) -> bool:
    q = normalize(query)
    return any(q.startswith(w) for w in OPEN_WORDS)


def has_open_words(query: str) -> bool:
    """开放问法可以出现在句首或句尾，如“有哪些报销制度”和“报销制度有哪些”。"""
    return has_any(normalize(query), OPEN_WORDS)


def has_negation(query: str) -> bool:
    q = normalize(query)
    if has_any(q, {word for word in NEGATION_WORDS if len(word) > 1}):
        return True
    if re.search(r"别(?:\s*|再|去|给|把)(?:报销|申请|提交|办理|审批|结算|开户|参加|使用|领取|发放|支付)", q):
        return True
    if re.search(
        r"不(?:能|可|得|准|允许|予|要|需|用|报|给|够|符合|通过|办理|申请|审批|"
        r"报销|结算|开户|提交|提供|支持|参加|参与|享受|发放|支付|领取|应该|应)",
        q,
    ):
        return True
    return bool(re.search(
        r"没(?:有|法|能|权限|通过|收到|拿到|办|报|领|申请|审批|提交|提供|"
        r"发放|支付|开通|成功|找到|看到)",
        q,
    ))


def has_comparison_words(query: str) -> bool:
    return has_any(normalize(query), COMPARISON_WORDS)


def has_time_words(query: str) -> bool:
    q = normalize(query)
    return has_any(q, TIME_WORDS) or bool(re.search(r"(?<!\d)(?:19|20)\d{2}年?(?!\d)", q))


def is_full_document_intent(query: str) -> bool:
    return has_any(normalize(query), FULL_DOC_WORDS)


def has_only_phenomenon_description(query: str) -> bool:
    q = normalize(query)
    has_phenomenon = has_any(q, PHENOMENON_WORDS)
    has_entity = has_specific_entity(query)
    if has_phenomenon and not has_entity and len(query) <= 25:
        return True
    if has_any(q, {"权限不够", "没权限", "审批卡住", "材料被退", "额度不够", "超标了"}) and not has_entity:
        return True
    return False


def has_business_word(query: str) -> bool:
    return has_specific_entity(query) or has_any(normalize(query), {
        "报销", "请假", "年假", "权限", "结算", "开户", "申请", "审批", "制度", "流程"
    })


# ============================================================
# 4. 实体提取
# ============================================================

def extract_entities(query: str, memory: Optional[SessionMemory] = None) -> Dict[str, List[str]]:
    """
    规则优先提取关键实体。
    返回格式：
    {
        "policy_name": [...],
        "doc_number": [...],
        "business_term": [...],
        "time": [...],
        "department": [...]
    }
    """
    entities = {
        "policy_name": [],
        "doc_number": [],
        "business_term": [],
        "time": [],
        "department": []
    }

    # 1. 别名映射
    for alias, standard in POLICY_ALIAS.items():
        if alias in query:
            entities["policy_name"].append(standard)

    # 2. 文号
    doc_numbers = re.findall(r"中债字〔?\d{4}〕?\d+号|第?\d+号", query)
    entities["doc_number"].extend(doc_numbers)

    # 3. 时间（年份动态识别，避免每年修改词表）
    entities["time"].extend(re.findall(r"(?<!\d)(?:19|20)\d{2}年?(?!\d)", query))
    for t in TIME_WORDS:
        if not re.fullmatch(r"\d{4}", t) and t in query:
            entities["time"].append(t)

    # 4. 简单业务术语（默认 + 可从 JSON config 的 business_terms 扩展）
    business_terms = ["DVP", "FOP", "RTGS", "中债估值", "债券账户", "名义持有人",
                      "年假", "事假", "报销", "VPN", "差旅"] + _EXTRA_BUSINESS_TERMS
    for term in business_terms:
        if term.lower() in query.lower():
            entities["business_term"].append(term)

    # 5. 会话继承：按槽位合并。新问题中显式出现的槽位优先，
    # 其他槽位仍从会话记忆继承，例如“那 2027 年呢”保留当前制度。
    if memory:
        if memory.current_policy and not entities["policy_name"]:
            entities["policy_name"].append(memory.current_policy)
        for k, v in memory.confirmed_entities.items():
            if v and not entities.get(k):
                entities[k] = list(v)

    # 去重
    for k in entities:
        entities[k] = list(dict.fromkeys(entities[k]))

    return entities


# ============================================================
# 5. 快速规则预过滤（决策树核心）
# ============================================================

def quick_rule_filter(query: str, memory: Optional[SessionMemory] = None) -> Optional[str]:
    """
    返回 query 类型，或 None（交给 LLM 分类）
    优先级从高到低
    """
    if not query or not query.strip():
        return "simple"

    q = query.strip()

    # 0. 全文意图
    if is_full_document_intent(q):
        return "full_document"

    # 1. multi_turn
    if has_pronoun(q) or (len(q) <= 6 and ends_with_question_particle(q)):
        return "multi_turn"

    # 2. comparison（必须在 multi_intent 之前，因为"和/与"可能同时触发两者）
    if has_comparison_words(q):
        return "comparison"

    # 3. multi_intent
    if count_question_marks(q) >= 2 or has_parallel_connector(q):
        return "multi_intent"

    # 4. negation
    if has_negation(q) and has_business_word(q):
        return "negation"

    # 5. time_sensitive
    if has_time_words(q):
        return "time_sensitive"

    # 6. too_specific
    if has_specific_entity(q) and has_troubleshooting_words(q):
        return "too_specific"

    # 6.5 suggestion — 基于某制度的优化/改进建议
    if has_any(normalize(q), SUGGESTION_WORDS) and (has_specific_entity(q) or has_business_word(q)):
        return "suggestion"

    # 7. colloquial
    if has_colloquial_words(q) or has_tone_particles(q):
        return "colloquial"

    # 8. semantic_gap — 短问题但已有明确实体/业务词时不算模糊
    if (len(q) <= 8 and not has_specific_entity(q) and not has_business_word(q)) \
       or has_only_phenomenon_description(q):
        return "semantic_gap"

    # 9. open_ended
    if has_open_words(q):
        return "open_ended"

    # 10. 默认：以上都不匹配 → 问题清晰明确
    return "simple"


# ============================================================
# 5.5 LLM 兜底分类（规则无法判断时调用）
# ============================================================

_LLM_CLASSIFY_PROMPT = """你是查询意图分类专家。请判断以下用户问题的类型，从下列类型中选择最匹配的一个：

- full_document: 用户想看某制度的全文/完整内容
- multi_turn: 多轮对话追问，含指代词（它/这个/那个）
- comparison: 对比两个或多个事物的区别
- multi_intent: 包含多个独立子问题
- negation: 含否定意图（不能/不可以/禁止）
- time_sensitive: 对时间敏感（最新/今年/现行）
- too_specific: 过于具体的操作/排障问题
- suggestion: 基于某制度提出优化/改进建议
- colloquial: 口语化表达
- semantic_gap: 问题模糊/简短/只有现象描述
- open_ended: 开放式列举问题
- simple: 以上都不匹配，问题清晰明确

只输出类型名称，不要解释。"""


def classify_query_with_llm(query: str, llm_call_fn) -> str:
    """用 LLM 判断 query 类型，返回类型字符串"""
    prompt = f"{_LLM_CLASSIFY_PROMPT}\n\n用户问题：{query}\n类型："
    result = llm_call_fn(prompt).strip().lower()
    valid_types = {
        "full_document", "multi_turn", "comparison", "multi_intent",
        "negation", "time_sensitive", "too_specific", "suggestion",
        "colloquial", "semantic_gap", "open_ended", "simple"
    }
    # 取第一个匹配的有效类型词
    for word in result.split():
        if word in valid_types:
            return word
    return "simple"  # LLM 输出异常时兜底


# ============================================================
# 5.6 领域门禁（内域 vs 百科）
# ============================================================

_DOMAIN_GATE_PROMPT = """你是中债公司（中央国债登记结算）内部政策问答的领域判断助手。
判断用户问题是否与法律、制度、公司业务相关（如制度条款、业务流程、报销、考勤、支付结算、监管法规、债券托管等）。
只回答"是"或"否"。"""


def _has_in_domain_signal(query: str, entities: Dict[str, Any]) -> bool:
    """规则预筛：有内域信号（制度名/文号/业务术语/业务词）则直接判内域，跳过 LLM。"""
    if entities.get("policy_name") or entities.get("doc_number") or entities.get("business_term"):
        return True
    return has_business_word(query)


async def check_domain(query: str, entities: Dict[str, Any], llm_call_fn: Optional[Callable] = None) -> bool:
    """领域门禁：True=内域（进入检索），False=出域（拒答）。

    规则预筛命中内域信号则跳过 LLM；否则 LLM 兜底判「是/否」。
    LLM 缺失/异常/输出非明确否定时 fail-open（判内域），不误伤正常问题。
    """
    if _has_in_domain_signal(query, entities):
        return True
    if llm_call_fn is None:
        return True
    prompt = f"{_DOMAIN_GATE_PROMPT}\n\n用户问题：{query}\n是否相关："
    try:
        raw = await llm_call_fn(prompt)
    except Exception:
        return True
    answer = (raw or "").strip()
    # 明确否定（否/不…）才判出域；其余（是/相关/空/异常）都 fail-open 判内域
    return not (answer and answer[0] in ("否", "不"))


# ============================================================
# 6. 改写策略（可对接真实 LLM，这里先给 Prompt 模板 + 规则兜底）
# ============================================================

def build_rewrite_prompt(
    query: str,
    qtype: str,
    entities: Dict,
    memory: SessionMemory,
    business_intents: Optional[List[str]] = None,
    rewrite_strategies: Optional[List[str]] = None,
) -> str:
    """根据类型生成改写 Prompt（实际调用时送给 LLM）"""
    history_text = memory.get_history_text()
    entity_text = str(entities)
    business_intent_text = ", ".join(business_intents or []) or "无"
    business_strategy_text = ", ".join(rewrite_strategies or []) or "无"

    base = f"""你是中债公司内部政策问答助手的查询优化模块。
请根据要求改写用户问题，使其更适合检索内部制度与业务规则。

对话历史：
{history_text}

已识别实体：
{entity_text}

已识别业务意图：
{business_intent_text}

业务改写策略：
{business_strategy_text}

用户原问题：
{query}
"""

    # --- Query 改写（统一策略，colloquial / multi_turn / simple 共用）---
    _QUERY_REWRITE_BASE = (
        "请将用户问题改写为适合文档检索的规范表达。"
        "要求：书面化、消除歧义、补全指代、保留核心意图。只输出改写后的问题。"
    )
    _QUERY_REWRITE_HINTS = {
        "multi_turn": "注意：当前问题是多轮对话的追问，请结合历史补全指代，改写成独立完整的检索语句。",
        "colloquial": "注意：问题偏口语化，请改成规范书面表述，补全可能的制度或业务实体。",
        "simple": "注意：问题已较清晰，只需轻微规范化（去掉语气词），不要过度扩展。",
    }

    # --- Multi-Query（扩展派，生成多个检索角度）---
    _MULTI_QUERY_BASE = (
        "请为以下问题生成 {n} 个不同角度的改写版本，用于文档检索。"
        "每行一个问题，不要编号，不要解释。"
        "必须将原始问题保留在第一行。"
    )

    strategy_map = {
        "full_document": "用户想看全文。请提取制度完整名称，并生成用于精确定位该制度的检索语句。保留「全文」意图。",
        "multi_intent": "问题包含多个意图。请拆解成多个独立的子问题，以JSON数组返回。",
        "negation": "问题含否定意图。改写时必须保留否定含义，不要变成正面表述。",
        "comparison": "这是对比问题。请输出简洁的检索语句，直接陈述对比对象和对比维度（如条件、天数、待遇等），保留对比语义，不要拆成独立的子问题，不要加「请说明」等前缀。",
        "time_sensitive": "问题有时间要求。改写时显式补上时间约束（如「最新」「2025年」「现行有效」）。",
        "too_specific": "问题涉及具体的业务操作或排障场景。请改写成清晰的检索语句，保留关键实体和操作意图。",
        "suggestion": "用户想基于现行制度提出优化建议。请提取制度完整名称，改写成检索该制度全文的语句，同时保留「优化/改进」意图。",
        "semantic_gap": _MULTI_QUERY_BASE.replace("{n}", "3"),
        "open_ended": _MULTI_QUERY_BASE.replace("{n}", "3"),
    }

    # 三种 Query 改写类型走统一策略
    if qtype in ("multi_turn", "colloquial", "simple"):
        hint = _QUERY_REWRITE_HINTS.get(qtype, "")
        instruction = _QUERY_REWRITE_BASE + "\n" + hint
    else:
        instruction = strategy_map.get(qtype, _QUERY_REWRITE_BASE)

    return base + f"\n改写要求：{instruction}\n\n请直接输出改写后的结果："


# ============================================================
# 6.5 HyDE（假设答案生成）
# ============================================================

# 不适合 HyDE 的模式：精确数字、编码、型号、日期
_HYDE_SKIP_PATTERNS = [
    r"\d{4}年\d{1,2}月\d{1,2}日",
    r"\d{4}-\d{2}-\d{2}",
    r"[A-Z]+[-_]\d{3,}",       # 编码/型号，如 DVP-001
    r"第?\d+号",
    r"\d{6,}",                  # 长数字串
]


def should_use_hyde(query: str, qtype: str, entities: Dict) -> bool:
    """
    决策规则：
    - 含精确数字、编码、文号 → 跳过
    - semantic_gap / open_ended（短 query、模糊概念）→ 最适合
    - simple 且 query 较短 → 可选
    - 其他类型 → 跳过
    """
    # 1. 精确实体 → 跳过
    for pattern in _HYDE_SKIP_PATTERNS:
        if re.search(pattern, query):
            return False

    # 2. suggestion 类型不适用 HyDE
    if qtype == "suggestion":
        return False

    # 3. 已有明确制度名/文号 → 跳过
    if entities.get("policy_name") or entities.get("doc_number"):
        return False

    # 4. 最适合的类型：短 query、模糊概念
    if qtype in ("semantic_gap", "open_ended"):
        return True

    # 5. simple 且短（≤15 字）→ 可选
    if qtype == "simple" and len(query.strip()) <= 15:
        return True

    return False


def build_hyde_prompt(query: str, entities: Dict) -> str:
    """生成 HyDE 的假设回答 prompt"""
    entity_text = str(entities) if any(entities.values()) else "无"
    return f"""你是中债公司内部政策专家。请为以下问题写一段假设性的回答，约150-200字。
不需要真实准确，只需要在语义上接近可能的真实答案文档。

已识别实体：{entity_text}

问题：{query}
假设回答："""


# ============================================================
# 6.6 Step-back（后退一步，抽象为原理性问题）
# ============================================================

def should_use_stepback(query: str, qtype: str, entities: Dict) -> bool:
    """
    决策规则：
    - 简单事实查询 → 跳过
    - full_document / negation / comparison / multi_intent → 跳过
    - 有具体实体/业务词 + 操作/排障类问题 → 最适合
    - too_specific 类型 → 必须启用
    """
    # 1. 不适合 Step-back 的类型
    if qtype in ("full_document", "negation", "comparison", "multi_intent", "time_sensitive"):
        return False

    # 2. too_specific / suggestion → 适合 Step-back
    if qtype in ("too_specific", "suggestion"):
        return True

    # 3. 有具体实体/业务词 → 后退到原理层
    if has_specific_entity(query) and len(query.strip()) >= 8:
        return True

    # 4. 含排障词 → 后退到通用处理原则
    if has_troubleshooting_words(query) and has_business_word(query):
        return True

    return False


def build_stepback_prompt(query: str, entities: Dict) -> str:
    """生成 Step-back 的抽象问题 prompt"""
    entity_text = str(entities) if any(entities.values()) else "无"
    return f"""你是中债公司内部政策专家。请将以下具体问题「后退一步」，生成一个更通用、更原理性的问题。
只输出改写后的问题，不要解释。

已识别实体：{entity_text}

具体问题：{query}
更通用的原理性问题："""


def rule_based_rewrite(query: str, qtype: str, entities: Dict, memory: SessionMemory) -> Dict[str, Any]:
    """
    规则兜底改写（无 LLM 时也能跑）。
    实际生产中建议用 LLM + 此规则作为 fallback。
    """
    result = {
        "type": qtype,
        "original": query,
        "rewritten": [query],
        "entities": entities,
        "need_full_doc": False,
        "filters": {}
    }

    # 保留并存的规则信号：全文请求也可同时指定年份/时效。
    intent_signals = []
    if is_full_document_intent(query):
        intent_signals.append("full_document")
    if has_time_words(query):
        intent_signals.append("time_sensitive")
        year_match = re.search(r"(?<!\d)((?:19|20)\d{2})年?(?!\d)", query)
        if year_match:
            result["filters"]["time"] = year_match.group(1)
        elif "最新" in query or "现行" in query:
            result["filters"]["time"] = "latest"
    if has_negation(query):
        intent_signals.append("negation")
    if has_comparison_words(query):
        intent_signals.append("comparison")
    if has_parallel_connector(query) or count_question_marks(query) >= 2:
        intent_signals.append("multi_intent")
    result["intent_signals"] = list(dict.fromkeys(intent_signals))

    # 补全别名
    for alias, standard in POLICY_ALIAS.items():
        if alias in query and standard not in query:
            query = query.replace(alias, standard)

    if qtype == "full_document":
        result["need_full_doc"] = True
        if memory.current_policy:
            result["rewritten"] = [f"{memory.current_policy} 全文"]
        elif entities.get("policy_name"):
            result["rewritten"] = [f"{entities['policy_name'][0]} 全文"]

    elif qtype == "multi_turn":
        # A follow-up may introduce a new slot (for example, "住宿标准")
        # while still referring to the active policy.  The topic switch check
        # above already clears memory when a different policy is explicit.
        if memory.current_policy:
            result["rewritten"] = [f"{memory.current_policy} {query}"]

    elif qtype == "time_sensitive":
        if "最新" in query or "现行" in query:
            result["filters"]["time"] = "latest"
        result["rewritten"] = [query]

    elif qtype == "suggestion":
        if entities.get("policy_name"):
            result["rewritten"] = [f"如何优化{entities['policy_name'][0]}"]
        else:
            result["rewritten"] = [query]

    elif qtype == "negation":
        # 保持原样，避免规则误改否定
        result["rewritten"] = [query]

    elif qtype == "simple":
        # 轻微清理语气词
        cleaned = query
        for p in TONE_PARTICLES:
            if cleaned.endswith(p):
                cleaned = cleaned[:-len(p)]
        result["rewritten"] = [cleaned.strip() or query]

    else:
        result["rewritten"] = [query]

    return result


def _parse_llm_rewrite(raw: Any, query: str, qtype: str,
                       entities: Dict, memory: SessionMemory) -> Dict[str, Any]:
    """Validate and normalize an LLM rewrite response.

    Multi-intent prompts require a JSON array, while multi-query prompts use
    one query per line.  Any malformed, empty, or otherwise unusable response
    falls back to the deterministic rule-based rewrite.
    """
    fallback = rule_based_rewrite(query, qtype, entities, memory)
    if not isinstance(raw, str):
        return fallback
    text = raw.strip()
    if not text:
        return fallback

    rewritten: List[str] = []
    if qtype == "multi_intent":
        # Models occasionally wrap JSON in a markdown code fence.
        candidate = text
        if candidate.startswith("```") and candidate.endswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate,
                               flags=re.IGNORECASE).strip()
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            return fallback
        if isinstance(parsed, list):
            rewritten = [item.strip()[:2000] for item in parsed
                         if isinstance(item, str) and item.strip()][:20]
        if not rewritten:
            return fallback
    elif qtype in ("semantic_gap", "open_ended"):
        # Strip optional list markers while preserving one query per line.
        for line in text.splitlines():
            line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
            if line:
                rewritten.append(line[:2000])
            if len(rewritten) >= 20:
                break
        if not rewritten:
            return fallback
    else:
        rewritten = [text[:2000]]

    fallback["rewritten"] = rewritten
    return fallback


def collect_retrieval_queries(understand_result: Dict[str, Any]) -> List[str]:
    """展平主查询、对比子结果及可选 HyDE/Step-back 查询并去重。"""
    queries: List[str] = []

    def add(value: Any):
        if isinstance(value, str):
            value = value.strip()
            if value and value not in queries:
                queries.append(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)

    add(understand_result.get("rewritten", []))
    for sub in understand_result.get("sub_results") or []:
        if isinstance(sub, dict):
            add(sub.get("rewritten", []))
    add(understand_result.get("hyde"))
    add(understand_result.get("stepback"))
    return queries


# ============================================================
# 6.7 决策解释（debug 用）
# ============================================================

def should_use_multiquery(query: str, qtype: str, entities: Dict) -> bool:
    """
    Multi-Query 决策规则：
    - 问题模糊、开放、多义 → 生成多个检索角度 → 合并去重
    - 问题已很精确 → 不需要（单路检索即可）
    """
    # semantic_gap / open_ended → 最适合
    if qtype in ("semantic_gap", "open_ended"):
        return True
    # 其他类型已有明确的改写策略，不需要多路检索
    return False


def _explain_multiquery(query: str, qtype: str, entities: Dict) -> str:
    if qtype == "semantic_gap":
        return f"启用：类型=semantic_gap，query 模糊/短/只有现象 → 多路覆盖不同语义区域"
    if qtype == "open_ended":
        return f"启用：类型=open_ended，开放式问题 → 多角度覆盖更全面"
    return f"跳过：类型={qtype} 已有明确的改写策略 → 单路检索即可"


def _explain_hyde(query: str, qtype: str, entities: Dict) -> str:
    """解释 HyDE 启用/跳过的原因"""
    # 检查精确模式
    for pattern in _HYDE_SKIP_PATTERNS:
        if re.search(pattern, query):
            return f"跳过：query 含精确数字/编码/日期 → HyDE 不适用"
    if entities.get("policy_name"):
        return f"跳过：已匹配制度名 {entities['policy_name']} → 直接检索更准"
    if entities.get("doc_number"):
        return f"跳过：已匹配文号 → 直接检索更准"
    if qtype == "suggestion":
        return f"跳过：类型=suggestion，已有制度名 → 直接检索该制度更准"
    if qtype in ("semantic_gap", "open_ended"):
        return f"启用：类型={qtype}，短 query/模糊概念 → HyDE 最适场景"
    if qtype == "simple" and len(query.strip()) <= 15:
        return f"启用：类型=simple，长度={len(query.strip())}≤15 → HyDE 可选"
    if qtype not in ("semantic_gap", "open_ended", "simple"):
        return f"跳过：类型={qtype} 不在 HyDE 适用范围内"
    if len(query.strip()) > 15:
        return f"跳过：类型=simple 但长度={len(query.strip())}>15 → query 已足够具体"
    return "跳过：不满足启用条件"


def _explain_stepback(query: str, qtype: str, entities: Dict) -> str:
    """解释 Step-back 启用/跳过的原因"""
    skip_types = ("full_document", "negation", "comparison", "multi_intent", "time_sensitive")
    if qtype in skip_types:
        return f"跳过：类型={qtype} 不适合 Step-back（事实/否定/对比/多意图/时效）"
    if qtype in ("too_specific", "suggestion"):
        return f"启用：类型={qtype} → 可后退到制度设计原则/问题处理框架"
    if has_specific_entity(query) and len(query.strip()) >= 8:
        return f"启用：含具体实体 + 长度={len(query.strip())}≥8 → 可后退到原理层"
    if has_troubleshooting_words(query) and has_business_word(query):
        return f"启用：含排障词 + 业务词 → 可后退到通用处理原则"
    if not has_specific_entity(query) and not has_troubleshooting_words(query):
        return f"跳过：无具体实体/排障词 → 无需后退抽象"
    return "跳过：不满足启用条件"


def _build_decision_trace(query: str, qtype: str, entities: Dict, decisions: Dict,
                         llm_calls: int = 0, llm_times: Dict[str, float] = None,
                         llm_total_ms: float = 0, elapsed_ms: float = 0,
                         actual: Dict[str, bool] = None) -> str:
    """构建单条 query 的决策链路文本"""
    has_entity = has_specific_entity(query)
    has_biz = has_business_word(query)
    has_ts = has_troubleshooting_words(query)

    if actual:
        flags = [f"{k}={'✓' if v else '✗'}" for k, v in actual.items()]
        status_line = f"  │ 实际执行: {' | '.join(flags)}"
    else:
        status_line = ""

    lines = [
        f"  ┌─ 决策链路 ─────────────────────────────",
    ]
    if status_line:
        lines.append(status_line)
    lines.extend([
        f"  │ 分类: {qtype}",
        f"  │ 特征: 实体={'✓' if has_entity else '✗'}  业务词={'✓' if has_biz else '✗'}  排障词={'✓' if has_ts else '✗'}  长度={len(query.strip())}",
        f"  │ 实体: policy={entities.get('policy_name') or '无'}  term={entities.get('business_term') or '无'}",
        f"  │ 改写策略: {'Multi-Query' if decisions.get('multiquery', {}).get('enabled') else 'Query 改写'} | {decisions.get('multiquery', {}).get('reason', '')}",
        f"  │ HyDE:     {'✓ 启用' if decisions['hyde']['enabled'] else '✗ 跳过'} | {decisions['hyde']['reason']}",
        f"  │ Step-back: {'✓ 启用' if decisions['stepback']['enabled'] else '✗ 跳过'} | {decisions['stepback']['reason']}",
    ])

    # LLM 调用详情
    if llm_times:
        parts = [f"{label}: {t:.0f}ms" for label, t in llm_times.items()]
        lines.append(f"  │ LLM: {llm_calls} 次 [{', '.join(parts)}] | LLM 合计: {llm_total_ms:.0f}ms | 总耗时: {elapsed_ms:.0f}ms")
    else:
        lines.append(f"  │ LLM: {llm_calls} 次 | 总耗时: {elapsed_ms:.0f}ms")

    lines.append(f"  └──────────────────────────────────────────")
    return "\n".join(lines)


# ============================================================
# 7. 主入口：完整理解流水线
# ============================================================

async def understand_query(
    query: str,
    memory: SessionMemory,
    use_llm_rewrite: bool = False,
    llm_call_fn=None,
    use_hyde: Optional[bool] = None,
    use_stepback: Optional[bool] = None,
    use_llm_classify: bool = False,
    debug: bool = False,
    _comparison_depth: int = 0,
    use_llm_business_classify: Optional[bool] = None,
    use_business_intents: bool = True,
    on_progress: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """
    完整 Query 理解流水线。

    参数：
        query: 用户当前问题
        memory: 会话记忆对象
        use_llm_rewrite: 是否调用 LLM 做改写
        llm_call_fn: LLM 调用函数，签名 fn(prompt: str) -> str
        use_hyde: HyDE 开关。None（默认）按 query 特征自动决策，False 显式关闭，True 强制允许自动决策。
        use_stepback: Step-back 开关。None（默认）按 query 特征自动决策，False 显式关闭，True 强制允许自动决策。
        use_llm_classify: 是否在规则判为 simple 时用 LLM 做更细分类
        use_llm_business_classify: 业务意图 LLM 兜底开关。None 时跟随
            use_llm_rewrite 自动启用；规则明确时即使启用也不会调用 LLM。
        debug: 是否打印决策过程

    返回：
        {
            "type": 问题类型,
            "original": 原问题,
            "rewritten": 改写后的 query 列表,
            "hyde": HyDE 假设答案（可能为 None）,
            "stepback": Step-back 抽象问题（可能为 None）,
            "decisions": {  # 仅 debug=True 时填充
                "hyde": {"enabled": bool, "reason": str},
                "stepback": {"enabled": bool, "reason": str},
            },
            "entities": 提取的实体,
            "need_full_doc": 是否全文意图,
            "filters": 额外过滤条件,
        }
    """
    t_start = time.time()
    llm_calls = 0
    llm_times: Dict[str, float] = {}  # 每次调用的耗时(ms)
    decisions = {}

    async def _call_llm(label: str, prompt: str) -> str:
        """封装 LLM 调用，记录耗时"""
        nonlocal llm_calls
        t0 = time.time()
        if on_progress is not None:
            try:
                await on_progress(label)
            except Exception:
                pass
        try:
            result = await llm_call_fn(prompt)
        except Exception:
            # LLM is an enhancement; deterministic rules must keep the
            # request available during provider failures or timeouts.
            result = ""
        llm_calls += 1
        llm_times[label] = (time.time() - t0) * 1000
        return result

    # 1. 规则分类
    qtype = quick_rule_filter(query, memory)

    # 1.5 LLM 分类覆盖（可选，规则=simple 时用 LLM 做更细分类）
    if use_llm_classify and qtype == "simple" and use_llm_rewrite and llm_call_fn is not None:
        raw = await _call_llm("分类", f"{_LLM_CLASSIFY_PROMPT}\n\n用户问题：{query}\n类型：")
        valid_types = {
            "full_document", "multi_turn", "comparison", "multi_intent",
            "negation", "time_sensitive", "too_specific", "suggestion",
            "colloquial", "semantic_gap", "open_ended", "simple"
        }
        for word in raw.strip().lower().split():
            if word in valid_types:
                qtype = word
                break

    # 2. 实体提取
    entities = extract_entities(query, memory)

    # 2.5 业务场景识别：与技术 qtype 正交，允许多标签。
    # 规则明确时 analyze_business_intents 不会调用传入的 LLM；只有规则
    # 无法确定时才走固定枚举 JSON 分类。
    # use_business_intents=False 时彻底跳过业务意图（规则 + LLM + 澄清门），代码保留。
    clarification_required = False
    if use_business_intents:
        business_llm_enabled = (
            llm_call_fn is not None
            and (
                use_llm_business_classify is True
                or (use_llm_business_classify is None and use_llm_rewrite)
            )
        )
        async def _business_llm_call(prompt: str) -> str:
            return await _call_llm("业务意图分类", prompt)

        business_llm_call = _business_llm_call if business_llm_enabled else None
        business_query = query
        contextual_followup = (
            qtype == "multi_turn"
            or bool(re.search(r"^(?:那|这|它|该|上述|前述)", query.strip()))
        )
        if contextual_followup and entities.get("policy_name"):
            current_policy = entities["policy_name"][0]
            if current_policy not in query:
                contextual_query = f"{current_policy} {query}"
                preliminary = await analyze_business_intents(query)
                explicit_ambiguity = (
                    preliminary["business_intents"] == ["ambiguous"]
                    and preliminary["intent_sources"].get("ambiguous") == "rule"
                )
                if not explicit_ambiguity:
                    business_query = contextual_query

        business_analysis = await analyze_business_intents(
            business_query, llm_call_fn=business_llm_call
        )
        business_plan = build_business_rewrite_plan(
            business_query, business_analysis["business_intents"]
        )
        # `ambiguous` may be a low-confidence safety fallback after an unavailable
        # or malformed classifier. Only an explicit rule/LLM ambiguity decision
        # pauses rewriting; fallback keeps the legacy technical strategy running.
        if (
            business_analysis["business_intents"] == ["ambiguous"]
            and business_analysis["intent_sources"].get("ambiguous") == "fallback"
        ):
            business_plan["clarification"] = {
                "required": False,
                "questions": [],
                "missing_slots": [],
            }
        clarification_required = business_plan["clarification"]["required"]
    else:
        business_analysis = {
            "business_intents": [],
            "intent_confidence": {},
            "intent_evidence": {},
            "intent_sources": {},
            "llm_business_classification_used": False,
        }
        business_plan = {
            "business_slots": {},
            "rewrite_strategies": [],
            "business_rewrites": [],
            "clarification": {"required": False, "questions": [], "missing_slots": []},
        }

    # 3. 换题检测
    if entities.get("policy_name") and memory.current_policy:
        if entities["policy_name"][0] != memory.current_policy:
            memory.clear_topic()

    # 4. 改写
    if use_llm_rewrite and llm_call_fn is not None and not clarification_required:
        prompt = build_rewrite_prompt(
            query,
            qtype,
            entities,
            memory,
            business_intents=business_analysis["business_intents"],
            rewrite_strategies=business_plan["rewrite_strategies"],
        )
        llm_result = await _call_llm("改写", prompt)
        result = _parse_llm_rewrite(llm_result, query, qtype, entities, memory)
        result.update({"hyde": None, "stepback": None})
    else:
        result = rule_based_rewrite(query, qtype, entities, memory)

    result.setdefault("hyde", None)
    result.setdefault("stepback", None)

    # 新字段只做加法，不改变旧版 type/rewritten/entities/filters 的语义。
    result.update(business_analysis)
    result.update(business_plan)

    # 5. Multi-Query 决策
    mq_ok = (
        should_use_multiquery(query, qtype, entities)
        if not clarification_required else False
    )
    mq_reason = _explain_multiquery(query, qtype, entities) if debug else ""
    decisions["multiquery"] = {"enabled": mq_ok, "reason": mq_reason}

    # 6. HyDE 决策
    hyde_ok = (
        should_use_hyde(query, qtype, entities)
        if use_hyde is not False and not clarification_required else False
    )
    hyde_reason = _explain_hyde(query, qtype, entities) if debug else ""
    decisions["hyde"] = {"enabled": hyde_ok, "reason": hyde_reason}

    if hyde_ok and use_llm_rewrite and llm_call_fn is not None:
        hyde_prompt = build_hyde_prompt(query, entities)
        result["hyde"] = await _call_llm("HyDE", hyde_prompt)

    # 7. 对比拆解（comparison → 子问题 + 各自走流程）
    sub_results = []
    if (qtype == "comparison" and _comparison_depth == 0
            and not clarification_required
            and use_llm_rewrite and llm_call_fn is not None):
        # 用 LLM 拆出两个对比对象的独立查询
        split_prompt = f"""请将以下对比问题拆解为两个独立的查询，分别检索每个对象的规定。
只输出两行，每行一个问题，不要编号，不要解释。

对比问题：{query}
独立查询："""
        split_raw = await _call_llm("拆解对比", split_prompt)
        if not isinstance(split_raw, str):
            split_raw = ""
        sub_queries = [s.strip() for s in split_raw.split("\n") if s.strip()][:2]
        # 每个子问题走一遍 understand_query（不再触发对比拆解，避免递归）
        for sq in sub_queries:
            sub = await understand_query(
                sq, memory,
                use_llm_rewrite=use_llm_rewrite, llm_call_fn=llm_call_fn,
                use_hyde=use_hyde, use_stepback=use_stepback,
                use_llm_classify=False,  # 子问题不重复分类
                debug=False,             # 子问题不打印链路
                _comparison_depth=_comparison_depth + 1,
                use_llm_business_classify=use_llm_business_classify,
            )
            sub_results.append(sub)

    # 9. Step-back 决策
    sb_ok = (
        should_use_stepback(query, qtype, entities)
        if use_stepback is not False and not clarification_required else False
    )
    sb_reason = _explain_stepback(query, qtype, entities) if debug else ""
    decisions["stepback"] = {"enabled": sb_ok, "reason": sb_reason}

    if sb_ok and use_llm_rewrite and llm_call_fn is not None:
        sb_prompt = build_stepback_prompt(query, entities)
        result["stepback"] = await _call_llm("Step-back", sb_prompt)

    # 汇总子问题调用数
    total_calls = llm_calls + sum(s["llm_calls"] for s in sub_results)
    total_llm_ms = sum(llm_times.values()) + sum(s["llm_total_ms"] for s in sub_results)
    elapsed_ms = (time.time() - t_start) * 1000
    result["llm_calls"] = total_calls
    result["llm_times"] = llm_times
    result["llm_total_ms"] = round(total_llm_ms)
    result["elapsed_ms"] = round(elapsed_ms)
    result["sub_results"] = sub_results if sub_results else None

    if debug:
        result["decisions"] = decisions
        # 实际执行状态（而非开关）
        actual = {
            "改写": True,  # 总是执行
            "HyDE": hyde_ok,
            "Step-back": sb_ok,
            "LLM分类": "分类" in llm_times,
        }
        result["_flags"] = actual
        result["_trace"] = _build_decision_trace(
            query, qtype, entities, decisions,
            llm_calls, llm_times, total_llm_ms, elapsed_ms, actual,
        )

    # 8. 更新记忆（先记录用户本轮）
    memory.add_turn("user", query)

    return result


# ============================================================
# 8. 改写系统提示词（生产：作为 system message 注入）
# ============================================================

QUERY_REWRITE_SYSTEM_PROMPT = """你是中债公司（中央国债登记结算）内部政策问答的查询改写专家。
目标：把员工口语化、指代不清、多意图的问题，改写成适合检索内部制度与业务规则的语句。

要求：
1. 不改变用户真实意图
2. 补全指代（它/这个 → 具体制度或业务名）
3. 否定问题必须保留否定含义
4. 时间词（最新/今年）要显式保留
5. 多意图时输出 JSON 数组，每个子问题独立
6. 只输出改写结果，不要解释过程"""

