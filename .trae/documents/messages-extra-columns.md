# messages 表新增 user_id / success / tokens 列 计划

## Summary
在 messages 表新增三列：`user_id`（问题是谁问的）、`success`（该条回答是否执行成功）、`tokens`（该条消息消耗的 token 数，按内容长度粗略估算）。涉及建表 SQL、DAO 层 INSERT/SELECT、chat_service 持久化构造、orchestrator_service 暴露 success 信号、SessionMessage 模型五处改动。

## Current State Analysis
- messages 表当前列：`id, session_id, role, content, timestamp, agent_ids`（[init_mysql.py:36-44](file:///workspace/app/dao/init_mysql.py)）
- 持久化入口：[chat_service.py:246-285](file:///workspace/app/services/chat_service.py) 构造 `new_messages`（user + assistant 两条），调 `session_service.append_messages`
- DAO 写入：[mysql_session_dao.py:236-242](file:///workspace/app/dao/mysql_session_dao.py) INSERT 仅写 `(session_id, role, content, timestamp, agent_ids)`
- DAO 读取：[mysql_session_dao.py:145-164](file:///workspace/app/dao/mysql_session_dao.py) SELECT 仅取 `role, content, timestamp, agent_ids`
- success 信号源：`orchestrator._last_results` 每个 TaskResult 含 `success` 字段；`orchestrator_service._last_orchestrator` 保存引用（[L623](file:///workspace/app/services/orchestrator_service.py)），已有 `last_agent_ids` property 但无 `last_success`
- 单 agent 路径在 [L536](file:///workspace/app/services/orchestrator_service.py) 设 `_last_agent_ids=[agent_id]`，需对称设 `_last_success`
- SessionMessage 模型在 [models/session.py:6-10](file:///workspace/app/models/session.py)

## Assumptions & Decisions
1. **列命名**：沿用 messages 表现有 snake_case 风格（session_id/agent_ids），用 `user_id` 而非 userId（action_audit 表的 userId 是历史遗留，不作为新列参照）
2. **success 语义**：user 消息 success=True（提问本身无执行成功与否概念，统一记 True）；assistant 消息 success=`orchestrator_service.last_success`
3. **last_success 派生（关键：基于 TaskResult.success 字段，而非"是否有返回"）**：编排返回 output 不代表成功。`_run_single_agent` 在 agent 执行异常/超时时设置 `TaskResult.success=False`（[base.py:134-136](file:///workspace/app/orchestrator/base.py) 异常分支、pipeline 超时 `_make_timeout_result`）。因此：
   - 多 agent 路径：`all(r.success for r in orchestrator._last_results)` —— 任一 agent 失败则整体 False；空列表时 True
   - 单 agent 路径：正常完成 True、异常 False（通过 `_last_success` 标志位）
   - **即使有 output 返回，只要 success=False 就记 False**
4. **tokens 估算（中英文分别乘系数）**：deepseek-v4 基于 BPE tokenizer，经验值：中文 1 字 ≈ 1.5 token，英文 4 字符 ≈ 1 token（0.25/字符）。封装为 `_estimate_tokens(content)`：
   ```python
   def _estimate_tokens(content: str) -> int:
       """按内容长度估算 token 数（deepseek-v4，中英文分别计系数，粗略估算）。"""
       if not content:
           return 0
       chinese_count = sum(1 for c in content if ord(c) > 127)
       ascii_count = len(content) - chinese_count
       return int(chinese_count * 1.5 + ascii_count * 0.25)
   ```
   user 和 assistant 消息都按各自 content 估算
5. **列类型**：`user_id VARCHAR(64)`、`success TINYINT(1) NOT NULL DEFAULT 1`、`tokens INT NOT NULL DEFAULT 0`
6. **兼容已部署环境**：末尾追加 `ALTER TABLE messages ADD COLUMN IF NOT EXISTS`（与 agent_ids 一致）

## Proposed Changes

### 1. [app/dao/init_mysql.py](file:///workspace/app/dao/init_mysql.py) — 建表 SQL
- messages 表 CREATE TABLE（L36-44）新增 3 列：`user_id VARCHAR(64) NOT NULL DEFAULT ''`、`success TINYINT(1) NOT NULL DEFAULT 1`、`tokens INT NOT NULL DEFAULT 0`，放在 `agent_ids` 之后
- 末尾 ALTER TABLE（L77-78）追加：
  ```sql
  ALTER TABLE messages ADD COLUMN IF NOT EXISTS (user_id VARCHAR(64) NOT NULL DEFAULT '');
  ALTER TABLE messages ADD COLUMN IF NOT EXISTS (success TINYINT(1) NOT NULL DEFAULT 1);
  ALTER TABLE messages ADD COLUMN IF NOT EXISTS (tokens INT NOT NULL DEFAULT 0);
  ```

### 2. [app/services/orchestrator_service.py](file:///workspace/app/services/orchestrator_service.py) — 新增 last_success
- `__init__` 初始化 `self._last_success = True`（与 `_last_agent_ids = []` 对称）
- 新增 `last_success` property（紧跟 `last_agent_ids` 之后，L177 附近）：
  ```python
  @property
  def last_success(self) -> bool:
      """获取最近一次编排是否全部成功。
      
      多 agent 路径从 _last_orchestrator._last_results 派生；
      单 agent 路径使用 _last_success 标志位。
      """
      if self._last_orchestrator and self._last_orchestrator._last_results:
          return all(r.success for r in self._last_orchestrator._last_results)
      return self._last_success
  ```
- 多 agent 路径（L614 重置处）加 `self._last_success = True`
- 单 agent 路径（L536 附近）设 `self._last_success = True`，异常分支设 `self._last_success = False`

### 3. [app/services/chat_service.py](file:///workspace/app/services/chat_service.py) — 持久化构造 + token 估算
- 顶部新增 `_estimate_tokens` 函数（L26 附近）：
  ```python
  def _estimate_tokens(content: str) -> int:
      """按内容长度估算 token 数（deepseek-v4，中英文分别计系数，粗略估算）。"""
      if not content:
          return 0
      chinese_count = sum(1 for c in content if ord(c) > 127)
      ascii_count = len(content) - chinese_count
      return int(chinese_count * 1.5 + ascii_count * 0.25)
  ```
- 持久化块（L262-279）构造 new_messages 时，user 消息加 `"user_id": user_id, "success": True, "tokens": _estimate_tokens(user_input)`；assistant 消息加 `"user_id": user_id, "success": orchestrator_service.last_success, "tokens": _estimate_tokens(final_output)`
- 异常时 `orchestrator_service.last_success` 访问失败兜底 `False`

### 4. [app/dao/mysql_session_dao.py](file:///workspace/app/dao/mysql_session_dao.py) — DAO 读写
- `append_messages` 的 INSERT messages（L236-242）加 3 列：
  ```sql
  INSERT INTO messages (session_id, role, content, timestamp, agent_ids, user_id, success, tokens) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
  ```
  参数追加 `msg.get("user_id", ""), int(bool(msg.get("success", True))), int(msg.get("tokens", 0))`
- `load_messages` 的 SELECT（L146）加 3 列：`SELECT role, content, timestamp, agent_ids, user_id, success, tokens FROM messages ...`
- `load_messages` 返回 dict 每条加 `"user_id": r.get("user_id", ""), "success": bool(r.get("success", 1)), "tokens": int(r.get("tokens", 0))`

### 5. [app/models/session.py](file:///workspace/app/models/session.py) — SessionMessage 模型
- `SessionMessage`（L6-10）新增 3 字段：`user_id: str = ""`、`success: bool = True`、`tokens: int = 0`

## Verification
1. `python -m py_compile` 对 5 个文件（init_mysql.py / orchestrator_service.py / chat_service.py / mysql_session_dao.py / models/session.py）
2. grep 核对：messages 表含 `user_id`/`success`/`tokens` 三列定义 + ALTER 语句；append_messages INSERT 含 8 个列名与 8 个 %s；load_messages SELECT 含新列；SessionMessage 含 3 新字段
3. git add + commit + push 到 `origin/trae/agent-5CYjia`
