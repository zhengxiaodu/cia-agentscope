# 改写器支持时间代词改写为当前日期

## 背景与目标

用户在对话中常使用"今天/今日/今年/近几个月/最近"等时间代词，改写器当前不会将其替换为具体日期，导致下游意图识别/智能体拿到的查询语义不完整（如"今天的新闻"→ 智能体不知道"今天"是哪天）。

目标：在改写提示词中注入服务器当前时间作为参数，让 LLM 自动把时间代词改写为具体日期/日期范围。

## 当前状态分析

### 改写器调用链
- [orchestrator_service.py:390-394](file:///workspace/app/services/orchestrator_service.py#L390-L394) 在每次 run 中构建 `QueryRewriter(client, model_config, rewrite_prompt=self._prompts.get("rewrite", ""))`
- [orchestrator_service.py:546](file:///workspace/app/services/orchestrator_service.py#L546) 调用 `rewriter.rewrite(user_input, history)`
- [rewriter.py:41-42](file:///workspace/app/intent/rewriter.py#L41-L42) **关键问题**：`if not history: return user_input` —— 无历史上下文直接返回，首条消息"今天天气"不会触发改写

### 占位符替换约定（已存在）
- [recognizer.py:85](file:///workspace/app/intent/recognizer.py#L85) 用 `prompt.replace("{{intents}}", self._intents_desc)`
- [react.py:190-192](file:///workspace/app/orchestrator/react.py#L190-L192) 用 `prompt.replace("{{task}}", ...)` 等
- [model_config.yml:39](file:///workspace/config/model_config.yml#L39) 注释已声明"支持 `{{变量}}` 占位符"，但 `prompts.rewrite` 当前无占位符

### 时区现状
- [config.py](file:///workspace/app/config.py) 无时区配置
- 服务器 sandbox 为 UTC，用户在 Asia/Shanghai

## 设计决策（已与用户确认）

1. **改写触发条件**：无 history 时，用正则检测 user_input 是否含时间代词；含则调 LLM 改写，不含则跳过（避免每条首条消息都增加 LLM 调用延迟）。有 history 时照常调 LLM。
2. **时区处理**：代码内固定 Asia/Shanghai（东八区），不新增 ENV 配置。
3. **时间代词正则**：覆盖常用词，详见下方列表。
4. **时间字符串格式**：`2026-07-13 星期一 14:30`（日期+中文星期+时分），让 LLM 能处理"今天/本周/本月/今年/现在"等各粒度。

## 改动清单

### 1. 修改 `/workspace/app/intent/rewriter.py`

**新增模块级常量与导入：**
```python
import re
from datetime import datetime, timezone, timedelta

# 东八区（Asia/Shanghai）
_SHANGHAI_TZ = timezone(timedelta(hours=8))

# 时间代词正则：覆盖常见中文时间指代词
_TIME_KEYWORDS_PATTERN = re.compile(
    r"今天|今日|明天|明日|后天|大后天|昨天|昨日|前天|大前天|"
    r"本周|这周|上周|下周|本周内|"
    r"本月|这个月|上个月|下个月|近几个月|最近几个月|"
    r"本季度|上季度|下季度|近几个季度|"
    r"今年|本年|去年|上一年|明年|下一年|近几年|"
    r"近几天|最近几天|近几周|最近几周|近几天内|"
    r"最近|近期|此刻|现在|当前|刚刚|刚才"
)
```

**新增辅助函数 `_get_current_date_str()`：**
```python
def _get_current_date_str() -> str:
    """获取 Asia/Shanghai 当前时间字符串，格式：2026-07-13 星期一 14:30。"""
    now = datetime.now(_SHANGHAI_TZ)
    weekday_cn = "星期一二三四五六日"[now.weekday()]
    return now.strftime(f"%Y-%m-%d {weekday_cn} %H:%M")
```

**修改 `rewrite()` 方法（核心逻辑变更）：**
```python
async def rewrite(self, user_input: str, history: Optional[List[dict]] = None) -> str:
    # 无上下文时，仅在含时间代词时才改写（避免每条首条消息都调 LLM）
    if not history:
        if not _TIME_KEYWORDS_PATTERN.search(user_input):
            return user_input

    # 拼接最近若干轮上下文摘要
    recent = history[-6:] if history else []
    context_str = "\n".join(
        [f"{m.get('role', 'user')}: {m.get('content', '')}" for m in recent]
    )

    user_prompt = (
        f"【历史对话上下文】\n{context_str}\n\n"
        f"【用户输入】\n{user_input}\n\n"
        f"请输出改写后的查询："
    )

    try:
        # 注入服务器当前时间到 system prompt
        system_prompt = self._rewrite_prompt.replace(
            "{{current_date}}", _get_current_date_str()
        )
        rewritten = await chat_complete(
            self._client,
            self._model_config,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        rewritten = rewritten.strip()
        if rewritten:
            logger.info(f"[QueryRewriter] 原始: {user_input} → 改写: {rewritten}")
            return rewritten
    except Exception:
        logger.exception("[QueryRewriter] 改写失败，使用原始输入")

    return user_input
```

关键变更点：
- 移除原 `if not history: return user_input` 的无条件跳过
- 改为：无 history 时用正则检测时间代词，含则继续调 LLM
- `recent = history[-6:] if history else []` —— 无 history 时为空列表，context_str 为空字符串
- 调 LLM 前用 `.replace("{{current_date}}", ...)` 注入当前时间

### 2. 修改 `/workspace/config/model_config.yml`

将 `prompts.rewrite` 模板更新为：
```yaml
  rewrite: |
    你是查询改写助手。请根据【历史对话上下文】把用户的口语化、省略、指代输入，改写为语义完整、可独立理解的标准查询。

    【服务器当前时间】
    {{current_date}}

    要求：
    1. 补全省略的主语、宾语、时间、对象（结合上下文中的实体）
    2. 消解代词指代（"它/这个/那个" → 具体指代对象）
    3. 时间代词改写：当用户输入包含"今天/今日/昨天/明天/本周/本月/今年/去年/最近/近期/近几个月"等时间代词时，必须根据【服务器当前时间】将其替换为具体的日期或日期范围（如"今天"→"2026-07-13"、"本周"→"2026-07-07至2026-07-13"、"近几个月"→"2026-04至2026-07"）
    4. 保持原意，不要扩展或新增用户未表达的需求
    5. 仅输出改写后的查询文本，不要任何解释
```

新增内容：
- `【服务器当前时间】\n{{current_date}}` 段落
- 第 3 条规则：明确要求 LLM 根据当前时间把时间代词替换为具体日期/日期范围，并给出示例

## 假设与边界

- 当前时间在每次 `rewrite()` 调用时实时获取（不缓存），保证准确性
- `{{current_date}}` 占位符若 prompt 中不存在，`.replace()` 是无副作用的 no-op，向后兼容
- 正则匹配是大小写敏感的中文匹配，不涉及英文时间词
- 无 history 且不含时间代词时，仍保持原降级逻辑（不调 LLM，直接返回原输入）
- 改写失败仍降级返回原输入（已有 try/except）

## 验证步骤

1. **静态校验**：`python -m py_compile app/intent/rewriter.py`
2. **grep 核对**：
   - `{{current_date}}` 在 model_config.yml 和 rewriter.py 中均出现
   - `_TIME_KEYWORDS_PATTERN`、`_get_current_date_str`、`_SHANGHAI_TZ` 在 rewriter.py 中定义
   - `timedelta(hours=8)` 确认东八区
3. **YAML 校验**：`python -c "import yaml; yaml.safe_load(open('config/model_config.yml'))"` 确保 yml 语法正确
4. **git 提交并 push** 到 `origin/trae/agent-5CYjia`

## 改动文件清单

- `/workspace/app/intent/rewriter.py`（修改：新增正则/时区/时间获取函数 + 修改 rewrite 逻辑）
- `/workspace/config/model_config.yml`（修改：rewrite prompt 加 `{{current_date}}` 占位符与时间改写规则）
