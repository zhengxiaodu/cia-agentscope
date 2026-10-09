# workspace-load 沙箱并行化 实施计划

**目标：** 把 `/chat` 的工作区准备从意图链路的前置串行步骤改成与之并行执行，并把每轮复用路径的沙箱命令往返从 3 次降到 1 次。

**架构：** `OrchestratorService._build_request_components` 按"是否依赖沙箱"拆成三个方法：配置读取与意图组件构建不碰沙箱、立即完成；工作区准备（get/create workspace + tools + registry）放进 `asyncio.create_task`，与"改写 → 意图识别 → 意图编排"并发跑，在选择编排器之前 `await`。`OpenSandboxWorkspaceManager` 侧把存活探测与 `mkdir` 合并成一条命令、把技能元数据按沙箱缓存、把 `list_skills` 的 N+1 次扫描并成一条 shell。

**技术栈：** Python 3.13 / FastAPI / asyncio / agentscope 2.0.5 / opensandbox SDK 0.1.15 / Langfuse v3 SDK（OTel context propagation）/ pytest

## 全局约束

*   设计依据：`workspace-load沙箱并行化-design.md`
    
*   **不碰技能装载链路**：`_inject_skills` 的路径解析、`ToolGroup` 的 `Skill` 类型契约、技能文件分发形态全部不改。`list_skills` 保持返回空列表的现状（沙箱内 `/workspace/skills/` 为空）
    
*   **不改生产配置值**：`.env` / `.env.example` 中 `OPENSANDBOX_POOL_SIZE` 等取值一律不动
    
*   **不改对外契约**：SSE 事件的 `type` 取值、字段名、错误文案 `环境准备失败: {e}` 全部保持不变
    
*   **不做 git 提交**：所有任务只修改文件并跑测试，提交由用户手动执行。任何步骤都不得出现 `git add` / `git commit`
    
*   测试命令：`.venv/bin/python -m pytest`
    
*   测试风格遵循 `tests/test_opensandbox_pip_env.py`：纯 mock、用 `__new__` 绕过 `__init__` 只填必需属性、`asyncio.run` 驱动、docstring 用中文写清"验证什么"
    
*   新增纯函数放 `app/services/workspace_file_access.py`（该模块已是 `build_*` / `parse_*` 纯函数的归属地）
    
*   日志前缀沿用各文件既有约定：manager 用 `[opensandbox_ws]`，orchestrator 用 `[OrchestratorService]`
    

---

## 文件结构

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `app/services/workspace_file_access.py` | 修改 | 新增 `build_list_skills_command()` 与 `parse_skills_output()` 两个纯函数 |
| `app/services/opensandbox_workspace_manager.py` | 修改 | `_Entry` 增加 `skills_meta`；新增 `_touch_session_dir()`；`get_workspace` / `create_workspace` 合并探活与建目录；`list_skills` 改单命令 + 缓存 + 删跨用户兜底 |
| `app/services/orchestrator_service.py` | 修改 | `_build_request_components` 拆成 `_load_config_bundle` / `_build_intent_components` / `_prepare_workspace_components`；新增 `_resolve_workspace_task`；`run()` 改并行时序与悬挂 task 清理 |
| `tests/test_workspace_file_access.py` | 修改 | 追加 list\_skills 命令构造与输出解析的测试 |
| `tests/test_opensandbox_ws_roundtrip.py` | 创建 | 沙箱往返次数、探活合并、技能元数据缓存、跨用户兜底收口 |
| `tests/test_orchestrator_component_split.py` | 创建 | 拆分后三个方法各自的行为（配置兜底、意图组件、工作区组件） |
| `tests/test_orchestrator_parallel_workspace.py` | 创建 | 并行时序、失败语义、悬挂 task 清理、agent\_id 直连分支 |
| `c-harness/docs/DEVELOPMENT.md` | 修改 | 记录预热池两层机制的配置口径 |
| `c-harness/docs/runbooks/2026-08-20-workspace-load并行化-verify.md` | 创建 | 真机验证 V1–V6 的可执行步骤 |

任务顺序：先做无依赖的纯函数（任务 1），再做 manager（任务 2、3），再做 orchestrator 的纯重构（任务 4），最后叠加并行（任务 5）。这样每一步的测试都能独立跑绿，且任务 5 出问题时可以只回退任务 5。

---

## 任务目标总览

动手前先读这一节，弄清任务各自为了什么。共同背景：Langfuse 实测一轮对话里 `workspace-load` 占 4.49s（其中真实新建沙箱 3.2s），但它排在意图链路（约 3.5s）之前串行等待，而工作区真正被消费要等到 agent 执行阶段；另外每轮复用路径固定发 3 次沙箱命令往返，实测单次往返 P50 约 1s。任务 1–3 削减往返次数，任务 4–5 让工作区准备与意图链路并行。

**任务 1（list\_skills 命令构造与输出解析）——技能扫描合并打地基。** 现在 `list_skills` 扫一轮要发 1 次 `ls` + 每个技能 1 次 `head`，串行 N+1 次沙箱往返。本任务先产出两个纯函数：`build_list_skills_command()` 构造一条能遍历技能根目录、输出"名称 TAB 描述首行"的 shell 命令；`parse_skills_output()` 把 stdout 解析回 dict 列表。纯函数不碰沙箱、可独立单测，任务 3 再把它接进 manager。

**任务 2（存活探测与 mkdir 合并）** 每轮复用固定发 3 次命令：`echo 1` 探活、`mkdir -p` 建会话目录、`ls` 扫技能。本任务把前两次合成一条 `_touch_session_dir()`（`exit_code == 0` 即存活），并给 `_Entry` 预留 `skills_meta` 字段（本任务只加字段，任务 3 才写入）。注意收益边界：这条优化只作用于"每轮复用"的热路径；冷启动路径（池命中 3 次、池未命中 2 次）的往返次数不变，冷启动的改善靠任务 5 的并行。

**任务 3（list\_skills 单命令化 + 缓存 + 跨用户兜底收口）**

三件事：用任务 1 的纯函数把 N+1 次扫描并成 1 次单命令；扫描结果缓存在 `_Entry.skills_meta`，同一沙箱第二轮不再扫；删掉"当前用户无 entry 时回落到其他用户沙箱"的兜底——那等于 A 用户的请求去读 B 用户的沙箱，是越权风险，无 entry 一律返回 `[]`。

**任务 4（组件构建拆分）——为并行做结构准备，行为不变。**

`_build_request_components` 是一个把"配置读取、意图组件、工作区组件"揉在一起的大方法，没法只把碰沙箱的那半边丢进后台任务。拆成 `_load_config_bundle` / `_build_intent_components` / `_prepare_workspace_components` 三个方法后，工作区准备才成为独立可调度的单元。本任务不改时序：`run()` 仍按原顺序串行调用三个方法，把"拆错了"和"并发引入的"这两类问题隔离开。

**任务 5（工作区准备与意图链路并行）——整个优化的核心收益。**

`_prepare_workspace_components` 丢进 `asyncio.create_task`，与"改写 → 意图识别 → 意图编排"并发跑，选编排器之前 `await`。4.49s 的工作区准备被约 3.5s 的意图链路"藏掉"，对总时长的净暴露只剩约 1s。同时守住两条边界：失败语义（错误事件晚于意图事件出现、文案 `环境准备失败: {e}` 不变）与悬挂 task 清理（意图链路先失败时后台任务不泄漏）。`agent_id` 直连路径没有可重叠的意图链路，立即 `await`，语义不变。

---

## 任务 1：list\_skills 的命令构造与输出解析（纯函数）

**文件：**

*   修改：`app/services/workspace_file_access.py`（在 `parse_find_output` 之后插入）
    
*   测试：`tests/test_workspace_file_access.py`（文件末尾追加）
    

**接口：**

*   消费：无
    
*   产出：
    
    *   `SKILLS_ROOT: str = "/workspace/skills"`
        
    *   `build_list_skills_command() -> str`
        
    *   `parse_skills_output(stdout: str) -> list[dict]`，每个 dict 形如 `{"name": str, "description": str, "directory": str}`
        
*   [ ] **步骤 1：写失败的测试**
    

追加到 `tests/test_workspace_file_access.py` 末尾：

```python
def test_build_list_skills_command_scans_skills_root_once():
    """一条命令要能遍历技能根目录、过滤非目录、并按 TAB 输出名称与描述首行。"""
    from app.services.workspace_file_access import build_list_skills_command

    cmd = build_list_skills_command()

    assert "/workspace/skills/*/" in cmd
    assert '[ -d "$d" ]' in cmd
    assert "head -1" in cmd
    assert "\t" in cmd


def test_parse_skills_output_normal_lines():
    """正常两列输出解析为 name/description/directory。"""
    from app.services.workspace_file_access import parse_skills_output

    stdout = "bocha_search\t---\nchart_renderer\t# 智能图表渲染 Skill\n"

    assert parse_skills_output(stdout) == [
        {
            "name": "bocha_search",
            "description": "---",
            "directory": "/workspace/skills/bocha_search",
        },
        {
            "name": "chart_renderer",
            "description": "# 智能图表渲染 Skill",
            "directory": "/workspace/skills/chart_renderer",
        },
    ]


def test_parse_skills_output_empty_description_falls_back_to_name():
    """SKILL.md 缺失或首行为空时，description 回落为技能名（与旧实现一致）。"""
    from app.services.workspace_file_access import parse_skills_output

    assert parse_skills_output("mineru\t\n") == [
        {
            "name": "mineru",
            "description": "mineru",
            "directory": "/workspace/skills/mineru",
        },
    ]


def test_parse_skills_output_ignores_blank_and_glob_literal():
    """空行与 glob 未匹配时残留的字面量 '*' 都不得进入结果。"""
    from app.services.workspace_file_access import parse_skills_output


    assert parse_skills_output("\n*\t\n  \n") == [ ]



def test_parse_skills_output_keeps_tabs_inside_description():
    """描述里含 TAB 时只按第一个 TAB 切分，描述整体保留。"""
    from app.services.workspace_file_access import parse_skills_output

    out = parse_skills_output("policy_qa\tdesc\twith\ttab\n")

    assert out[0]["description"] == "desc\twith\ttab"

```

*   [ ] **步骤 2：跑测试确认失败**
    

运行：`.venv/bin/python -m pytest tests/test_workspace_file_access.py -v -k "list_skills or skills_output"` 预期：FAIL，`ImportError: cannot import name 'build_list_skills_command'`

*   [ ] **步骤 3：实现两个纯函数**
    

在 `app/services/workspace_file_access.py` 的 `parse_find_output` 函数之后插入：

```python
SKILLS_ROOT = "/workspace/skills"


def build_list_skills_command(skills_root: str = SKILLS_ROOT) -> str:
    """构造一次性列出全部技能名与描述首行的命令（单次沙箱往返）。

    输出每行 `名称<TAB>描述首行`。`[ -d "$d" ]` 用于过滤 glob 未匹配时残留的
    字面量路径；`head -1` 与旧实现的 `head -5` 取首行等价。
    """
    return (
        f'for d in {skills_root}/*/; do [ -d "$d" ] || continue; '
        'n=$(basename "$d"); h=$(head -1 "$d/SKILL.md" 2>/dev/null); '
        "printf '%s\t%s\n' \"$n\" \"$h\"; done"
    )


def parse_skills_output(stdout: str, skills_root: str = SKILLS_ROOT) -> list[dict]:
    """解析 build_list_skills_command 的输出为技能元信息列表。

    - 只按第一个 TAB 切分，描述内的 TAB 原样保留
    - 描述为空时回落为技能名（与旧实现一致）
    - 跳过空行与 glob 字面量 '*'
    """

    skills: list[dict] = [ ]

    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        name, _, desc = line.partition("\t")
        name = name.strip()
        if not name or name == "*":
            continue
        desc = desc.strip()
        skills.append({
            "name": name,
            "description": desc or name,
            "directory": f"{skills_root}/{name}",
        })
    return skills

```

*   [ ] **步骤 4：跑测试确认通过**
    

运行：`.venv/bin/python -m pytest tests/test_workspace_file_access.py -v` 预期：全部 PASS（含该文件原有测试）

*   [ ] **步骤 5：交付校验**
    

运行：`.venv/bin/python -m pytest` 预期：全绿。**不要提交，交由用户手动提交。**

---

## 任务 2：存活探测与 mkdir 合并（每轮省 1 次往返）

**文件：**

*   修改：`app/services/opensandbox_workspace_manager.py`（`_Entry`、新增 `_touch_session_dir`、`get_workspace`、`create_workspace`）
    
*   测试：`tests/test_opensandbox_ws_roundtrip.py`（创建）
    

**接口：**

*   消费：无
    
*   产出：
    
    *   `_Entry.skills_meta: list[dict] | None`（本任务只加字段与初值，任务 3 才使用）
        
    *   `OpenSandboxWorkspaceManager._touch_session_dir(sbx, session_id) -> bool`：一条命令同时建会话目录与判断存活，`exit_code == 0` 为存活，异常一律 `False`
        
*   [ ] **步骤 1：写失败的测试**
    

创建 `tests/test_opensandbox_ws_roundtrip.py`：

```python
"""OpenSandboxWorkspaceManager 的沙箱命令往返次数与存活判定（纯 mock）。

不依赖真实 opensandbox-server：用 _FakeSandbox 记录每一条 commands.run，
断言往返次数与命令内容。
"""
import asyncio

import pytest

from app.services.opensandbox_workspace_manager import (
    OpenSandboxWorkspaceManager,
    _Entry,
)

BASEDIR = "/data/workspaces"


class _Msg:
    def __init__(self, text: str):
        self.text = text


class _Logs:
    def __init__(self, text: str):
        self.stdout = [_Msg(text)]


class _Result:
    def __init__(self, exit_code: int, stdout: str):
        self.exit_code = exit_code
        self.logs = _Logs(stdout)


class _FakeSandbox:
    """记录全部命令；stdout_map 按子串匹配返回对应 stdout。"""

    def __init__(self, exit_code: int = 0, stdout_map: dict | None = None):
        self.id = "sbx-1"
        self.workdir = ""

        self.calls: list[str] = [ ]

        self.destroyed = False
        self._exit_code = exit_code
        self._stdout_map = stdout_map or {}
        self.commands = self

    async def run(self, cmd: str):
        self.calls.append(cmd)
        stdout = ""
        for key, val in self._stdout_map.items():
            if key in cmd:
                stdout = val
        return _Result(self._exit_code, stdout)

    async def destroy(self):
        self.destroyed = True


def _make_manager() -> OpenSandboxWorkspaceManager:
    """只填 get_workspace / create_workspace / list_skills 用到的属性。"""
    mgr = OpenSandboxWorkspaceManager.__new__(OpenSandboxWorkspaceManager)
    mgr._basedir = BASEDIR
    mgr._ttl = 3600.0
    mgr._cache = {}
    mgr._locks = {}
    mgr._struct_lock = asyncio.Lock()
    return mgr


def _seed(mgr: OpenSandboxWorkspaceManager, sbx: _FakeSandbox, user_id: str = "u1") -> _Entry:
    entry = _Entry(sbx, user_id, session_ids={"s0"})
    mgr._cache[f"user-{user_id}"] = entry
    return entry


def test_get_workspace_warm_path_issues_single_command():
    """复用路径每轮只发一条命令：mkdir 与存活探测合并。"""
    sbx = _FakeSandbox()
    mgr = _make_manager()
    _seed(mgr, sbx)

    ws = asyncio.run(mgr.get_workspace("u1", "s1"))

    assert ws is sbx
    assert len(sbx.calls) == 1
    assert sbx.calls[0] == f"mkdir -p {BASEDIR}/s1 && echo 1"


def test_get_workspace_sets_session_dir_on_sandbox():
    """workdir 仍要写回 Sandbox 对象与 entry（AgentRegistry 依赖它构建 system_prompt）。"""
    sbx = _FakeSandbox()
    mgr = _make_manager()
    entry = _seed(mgr, sbx)

    asyncio.run(mgr.get_workspace("u1", "s1"))

    assert sbx.workdir == f"{BASEDIR}/s1"
    assert entry.workdir == f"{BASEDIR}/s1"
    assert "s1" in entry.session_ids


def test_get_workspace_evicts_when_command_fails():
    """合并命令 exit_code != 0 → 判定沙箱已死，淘汰并返回 None。"""
    sbx = _FakeSandbox(exit_code=1)
    mgr = _make_manager()
    _seed(mgr, sbx)

    ws = asyncio.run(mgr.get_workspace("u1", "s1"))

    assert ws is None
    assert sbx.destroyed is True
    assert mgr._cache == {}


def test_touch_session_dir_returns_false_on_exception():
    """命令抛异常（连接断开）也算已死，不得向上冒泡。"""
    class _Boom(_FakeSandbox):
        async def run(self, cmd: str):
            raise RuntimeError("connection reset")

    mgr = _make_manager()

    assert asyncio.run(mgr._touch_session_dir(_Boom(), "s1")) is False


def test_entry_has_skills_meta_defaulting_to_none():
    """_Entry 新增 skills_meta 字段，初值 None（Task 3 用它做缓存）。"""
    entry = _Entry(_FakeSandbox(), "u1")

    assert entry.skills_meta is None

```

*   [ ] **步骤 2：跑测试确认失败**
    

运行：`.venv/bin/python -m pytest tests/test_opensandbox_ws_roundtrip.py -v` 预期：FAIL。`test_entry_has_skills_meta_defaulting_to_none` 报 `AttributeError: skills_meta`；`test_get_workspace_warm_path_issues_single_command` 报 `assert 2 == 1`（当前是 `echo 1` + `mkdir -p` 两条）

*   [ ] **步骤 3：改** 
    

`app/services/opensandbox_workspace_manager.py` 中，把 `_Entry` 整体替换为：

```python
class _Entry:
    __slots__ = (
        "sandbox", "last_access", "user_id", "session_ids", "workdir", "skills_meta",
    )

    def __init__(self, sandbox: Sandbox, user_id: str, session_ids: set | None = None):
        self.sandbox = sandbox
        self.last_access = time.monotonic()
        self.user_id = user_id
        self.session_ids = session_ids or set()
        self.workdir = "/data/workspaces"
        # 技能元信息缓存：技能只在沙箱创建时注入，沙箱生命周期内不变，
        # None 表示尚未扫描。见 list_skills。
        self.skills_meta: list[dict] | None = None

```

在 `_is_sandbox_alive` 方法之后插入：

```python
    async def _touch_session_dir(self, sbx: Sandbox, session_id: str) -> bool:
        """建会话目录并顺带探活：一条命令同时完成 mkdir 与存活判断。

        exit_code == 0 → 沙箱存活且目录就绪；非 0 或抛异常 → 视为沙箱已死。
        把两次往返压成一次，是每轮复用路径的主要开销来源。
        """
        session_dir = self._session_dir(session_id)
        try:
            result = await sbx.commands.run(f"mkdir -p {session_dir} && echo 1")
            return result.exit_code == 0
        except Exception:
            return False

```

`_is_sandbox_alive` **保留不动**——`_acquire_from_pool` 仍在用它（池内沙箱此时没有会话目录，没有可合并的命令）。

*   [ ] **步骤 4：改** 
    

把 `get_workspace` 中从 `# 高可用：探测沙箱存活` 到 `return entry.sandbox` 的整段替换为：

```python
            # 合并探活与建目录：一次往返同时判断存活并准备会话目录
            if not await self._touch_session_dir(entry.sandbox, session_id):
                logger.warning(f"[opensandbox_ws] 沙箱已崩溃，淘汰 wid={wid}")
                await self._evict_locked(wid)
                return None
            session_dir = self._session_dir(session_id)
            entry.last_access = time.monotonic()
            entry.session_ids.add(session_id)
            entry.workdir = session_dir
            # 在 Sandbox 对象上设置 workdir，兼容 AgentRegistry 的 system_prompt 构建
            entry.sandbox.workdir = session_dir
            return entry.sandbox

```

*   [ ] **步骤 5：改** 
    

复用分支：把 `if not await self._is_sandbox_alive(entry.sandbox):` 起的 if/else 整段替换为：

```python
                if not await self._touch_session_dir(entry.sandbox, session_id):
                    logger.warning(
                        f"[opensandbox_ws] 沙箱已崩溃，重建 wid={wid}"
                    )
                    await self._evict_locked(wid)
                else:
                    entry.last_access = time.monotonic()
                    session_dir = self._session_dir(session_id)
                    entry.session_ids.add(session_id)
                    entry.workdir = session_dir
                    # 在 Sandbox 对象上设置 workdir，兼容 AgentRegistry 的 system_prompt 构建
                    entry.sandbox.workdir = session_dir
                    logger.info(f"[opensandbox_ws] 复用沙箱 wid={wid} session={session_id}")
                    return entry.sandbox

```

新建分支：把 `session_dir = self._session_dir(session_id)` 与 `await sbx.commands.run(f"mkdir -p {session_dir}")` 两行替换为：

```python
            session_dir = self._session_dir(session_id)
            if not await self._touch_session_dir(sbx, session_id):
                logger.warning(
                    f"[opensandbox_ws] 新建沙箱后会话目录准备失败 sandbox_id={sbx.id}"
                )

```

保持现有容错语义：目录准备失败只记 warning，不抛（与改动前 `commands.run` 不检查返回值一致）。

*   [ ] **步骤 6：跑测试确认通过**
    

运行：`.venv/bin/python -m pytest tests/test_opensandbox_ws_roundtrip.py -v` 预期：全部 PASS

*   [ ] **步骤 7：交付校验**
    

运行：`.venv/bin/python -m pytest` 预期：全绿。**不要提交。**

---

## 任务 3：list\_skills 单命令化 + 按沙箱缓存 + 跨用户兜底收口

**文件：**

*   修改：`app/services/opensandbox_workspace_manager.py`（import 段、`list_skills`）
    
*   测试：`tests/test_opensandbox_ws_roundtrip.py`（追加）
    

**接口：**

*   消费：任务 1 的 `build_list_skills_command()` / `parse_skills_output()`；任务 2 的 `_Entry.skills_meta`
    
*   产出：`list_skills(user_id="", session_id="") -> list[dict]` 语义变为：无 entry 返回 `[]`（不再回落到其他用户的沙箱）；首次扫描后结果缓存在 `_Entry.skills_meta`
    
*   [ ] **步骤 1：写失败的测试**
    

追加到 `tests/test_opensandbox_ws_roundtrip.py` 末尾：

```python
_SKILLS_STDOUT = "bocha_search\t---\nmineru\t# MinerU 文档解析技能\n"


def test_list_skills_uses_single_command():
    """扫描技能只发一条命令，不再是 1 次 ls + 每技能 1 次 head。"""
    sbx = _FakeSandbox(stdout_map={"/workspace/skills": _SKILLS_STDOUT})
    mgr = _make_manager()
    _seed(mgr, sbx)

    skills = asyncio.run(mgr.list_skills(user_id="u1", session_id="s1"))

    assert len(sbx.calls) == 1
    assert [s["name"] for s in skills] == ["bocha_search", "mineru"]
    assert skills[1]["directory"] == "/workspace/skills/mineru"


def test_list_skills_second_call_hits_cache():
    """同一沙箱第二次调用不再发命令（技能只在创建时注入，缓存即事实）。"""
    sbx = _FakeSandbox(stdout_map={"/workspace/skills": _SKILLS_STDOUT})
    mgr = _make_manager()
    entry = _seed(mgr, sbx)

    async def _twice():
        first = await mgr.list_skills(user_id="u1")
        second = await mgr.list_skills(user_id="u1")
        return first, second

    first, second = asyncio.run(_twice())

    assert len(sbx.calls) == 1
    assert first == second
    assert entry.skills_meta == first


def test_list_skills_exception_returns_empty_without_caching():

    """扫描失败返回 []，且不写缓存——下一轮还能重试。"""

    class _Boom(_FakeSandbox):
        async def run(self, cmd: str):
            raise RuntimeError("exec failed")

    mgr = _make_manager()
    entry = _seed(mgr, _Boom())


    assert asyncio.run(mgr.list_skills(user_id="u1")) == [ ]

    assert entry.skills_meta is None


def test_list_skills_without_entry_returns_empty_not_other_user_sandbox():

    """无该用户的沙箱时返回 []，绝不回落到别人的沙箱（跨用户技能清单泄漏）。"""

    other = _FakeSandbox(stdout_map={"/workspace/skills": _SKILLS_STDOUT})
    mgr = _make_manager()
    _seed(mgr, other, user_id="someone_else")


    assert asyncio.run(mgr.list_skills(user_id="u1")) == [ ]


    assert asyncio.run(mgr.list_skills(user_id="")) == [ ]


    assert other.calls == [ ]


```

*   [ ] **步骤 2：跑测试确认失败**
    

运行：`.venv/bin/python -m pytest tests/test_opensandbox_ws_roundtrip.py -v -k list_skills`

预期：FAIL。`test_list_skills_uses_single_command` 报 `assert 2 == 1`（当前 ls + head）；`test_list_skills_without_entry_returns_empty_not_other_user_sandbox` 报 `assert [...] == []`（当前会回落到第一个活跃沙箱）

*   [ ] **步骤 3：扩展 import**
    

`app/services/opensandbox_workspace_manager.py` 顶部的 `from app.services.workspace_file_access import (...)` 块中追加两项：

```python
from app.services.workspace_file_access import (
    build_find_command,
    parse_find_output,
    safe_rel_path,
    build_base64_read_command,
    build_stat_command,
    build_list_skills_command,
    parse_skills_output,
)

```

*   [ ] **步骤 4：重写** 
    

把 `list_skills` 整个方法体替换为：

```python
    async def list_skills(self, user_id: str = "", session_id: str = "") -> list[dict]:
        """列出沙箱内已注入的技能元数据（单次往返 + 按沙箱缓存）。

        一条 shell 命令扫完 /workspace/skills/ 并输出 `名称<TAB>描述首行`，
        结果缓存在 _Entry.skills_meta：技能只在沙箱创建时注入，沙箱生命周期内不变。

        无该用户的沙箱时返回 []——不得回落到其他用户的沙箱（技能清单跨用户泄漏）。

        返回格式兼容 agentscope workspace.list_skills() 的 dict 列表。

        session_id 保留在签名中仅为调用方兼容，本方法不使用。
        """
        entry = self._entry_for(user_id)
        if entry is None:

            return [ ]

        if entry.skills_meta is not None:
            return entry.skills_meta

        try:
            result = await entry.sandbox.commands.run(build_list_skills_command())
            skills = parse_skills_output(self._stdout(result))
        except Exception:
            logger.exception("[opensandbox_ws] list_skills 失败")

            return [ ]

        entry.skills_meta = skills
        return skills

```

`_entry_for` 与 `_stdout` 已存在于本文件，无需新增。

*   [ ] **步骤 5：跑测试确认通过**
    

运行：`.venv/bin/python -m pytest tests/test_opensandbox_ws_roundtrip.py -v` 预期：全部 PASS

*   [ ] **步骤 6：交付校验**
    

运行：`.venv/bin/python -m pytest` 预期：全绿。**不要提交。**

---

## 任务 4：请求级组件构建拆分（纯重构，行为不变）

**文件：**

*   修改：`app/services/orchestrator_service.py`（删除 `_build_request_components`，新增三个方法）
    
*   测试：`tests/test_orchestrator_component_split.py`（创建）
    

**接口：**

*   消费：无（`_load_cached_user_config` / `_fuse_user_config` 已存在）
    
*   产出：
    
    *   `async _load_config_bundle(user_id: str, redis_client) -> dict`，键固定为 `merged_intents` / `merged_agents` / `merged_skills` / `default_orchestration`
        
    *   `_build_intent_components(fused: dict) -> tuple`（返回 `(rewriter, recognizer)`，同步方法）
        
    *   `async _prepare_workspace_components(fused, user_id_safe, session_id_safe, search_enabled=True, skills=None, langfuse_service=None) -> tuple`（返回 `(registry, agent_factory)`）
        
    *   `_build_request_components` **被删除**，`run()` 在任务 5 改为直接编排这三个方法
        

本任务只搬迁代码、不改行为：`run()` 里先按原有顺序依次调用三个新方法，保持串行。并行留给任务 5，这样出问题时能区分"拆错了"和"并发引入的"。

*   [ ] **步骤 1：写失败的测试**
    

创建 `tests/test_orchestrator_component_split.py`：

```python
"""_build_request_components 拆分后的三个方法各自的行为（纯 mock）。

只验证拆分不改语义：配置读取的命中/兜底、意图组件的装配、
工作区组件对 get_workspace → create_workspace 的回退顺序。
"""
import json

import pytest

from app.services.orchestrator_service import OrchestratorService

FUSED = {
    "merged_intents": [
        {"id": "general_chat", "description": "闲聊", "agent": "general_agent"},
    ],
    "merged_agents": [

        {"id": "general_agent", "name": "通用助手", "skills": [], "system_prompt": "你是助手"},

    ],
    "merged_skills": [
        {"name": "bocha_search", "directory": "/workspace/skills/bocha_search"},
    ],
    "default_orchestration": {"relation": "independent"},
}


class _FakeRedis:
    def __init__(self, payload: dict | None):
        self._payload = payload

    async def get(self, key: str):
        if self._payload is None:
            return None
        return json.dumps(self._payload).encode("utf-8")


class _FakeWorkspaceManager:
    """记录调用顺序；get_workspace 返回 None 时应回退到 create_workspace。"""

    def __init__(self, existing=None):

        self.calls: list[str] = [ ]

        self._existing = existing
        self.created_skill_dirs = None

    async def get_workspace(self, user_id, session_id):
        self.calls.append("get")
        return self._existing

    async def create_workspace(self, user_id, session_id, skill_dirs=None, langfuse_service=None):
        self.calls.append("create")
        self.created_skill_dirs = skill_dirs
        return _FakeSandbox()

    async def list_skills(self, user_id="", session_id=""):
        self.calls.append("list_skills")
        return [
            {"name": "bocha_search", "description": "搜索", "directory": "/workspace/skills/bocha_search"},
            {"name": "mineru", "description": "解析", "directory": "/workspace/skills/mineru"},
        ]


class _FakeSandbox:
    def __init__(self):
        self.id = "sbx-1"
        self.workdir = "/data/workspaces/s1"
        self.workspace_id = "user-u1"


def _make_service(workspace_manager=None) -> OrchestratorService:
    """只填三个新方法用到的属性。"""
    svc = OrchestratorService.__new__(OrchestratorService)
    svc._model_config = {"models": {"default": {}}}
    svc._prompts = {
        "rewrite": "改写提示词",
        "intent_recognition": "识别提示词",
        "intent_orchestration": "编排提示词",
    }
    svc._intent_client = object()
    svc._intent_model_cfg = {"model": "qwen-plus"}
    svc._workspace_manager = workspace_manager
    svc._last_success = True
    return svc


async def test_load_config_bundle_uses_cache_when_present():
    """Redis 命中 → 直接返回缓存内容，四个键齐全。"""
    svc = _make_service()

    bundle = await svc._load_config_bundle("u1", _FakeRedis(FUSED))

    assert bundle == FUSED


async def test_load_config_bundle_falls_back_to_base_only_on_miss():
    """Redis 未命中 → 走 base-only 融合（不请求 mng），键仍齐全。"""
    svc = _make_service()

    bundle = await svc._load_config_bundle("u1", _FakeRedis(None))

    assert set(bundle) == {
        "merged_intents", "merged_agents", "merged_skills", "default_orchestration",
    }
    # base-only 兜底读的是仓库内 YAML，技能配置非空
    assert bundle["merged_skills"]


async def test_load_config_bundle_without_redis_returns_base_only():
    """redis_client 为 None 时同样兜底，不抛异常。"""
    svc = _make_service()

    bundle = await svc._load_config_bundle("u1", None)

    assert bundle["merged_agents"]


def test_build_intent_components_wires_prompts():
    """改写器与识别器拿到对应的 prompt 与 default_orchestration。"""
    svc = _make_service()

    rewriter, recognizer = svc._build_intent_components(FUSED)

    assert rewriter._rewrite_prompt == "改写提示词"
    assert recognizer._recognition_prompt == "识别提示词"
    assert recognizer._orchestration_prompt == "编排提示词"
    assert recognizer._default_orchestration == {"relation": "independent"}


async def test_prepare_workspace_components_creates_when_absent(monkeypatch):
    """get_workspace 返回 None → 回退 create_workspace，并透传技能目录。"""
    monkeypatch.setattr(
        "app.services.orchestrator_service.WORKSPACE_BACKEND", "opensandbox"
    )
    mgr = _FakeWorkspaceManager(existing=None)
    svc = _make_service(mgr)

    registry, factory = await svc._prepare_workspace_components(
        fused=FUSED,
        user_id_safe="u1",
        session_id_safe="s1",
        search_enabled=True,

        skills=[],

        langfuse_service=None,
    )

    assert mgr.calls == ["get", "create", "list_skills"]
    assert mgr.created_skill_dirs == ["/workspace/skills/bocha_search"]
    assert registry.get_definition("general_agent") is not None
    assert factory is not None


async def test_prepare_workspace_components_reuses_existing(monkeypatch):
    """get_workspace 命中 → 不调 create_workspace。"""
    monkeypatch.setattr(
        "app.services.orchestrator_service.WORKSPACE_BACKEND", "opensandbox"
    )
    mgr = _FakeWorkspaceManager(existing=_FakeSandbox())
    svc = _make_service(mgr)

    await svc._prepare_workspace_components(
        fused=FUSED, user_id_safe="u1", session_id_safe="s1",

        search_enabled=True, skills=[], langfuse_service=None,

    )

    assert mgr.calls == ["get", "list_skills"]


async def test_prepare_workspace_components_filters_search_skill(monkeypatch):
    """search_enabled=False → bocha_search 从 all_skills_meta 中剔除。"""
    monkeypatch.setattr(
        "app.services.orchestrator_service.WORKSPACE_BACKEND", "opensandbox"
    )
    mgr = _FakeWorkspaceManager(existing=_FakeSandbox())
    svc = _make_service(mgr)

    registry, _ = await svc._prepare_workspace_components(
        fused=FUSED, user_id_safe="u1", session_id_safe="s1",

        search_enabled=False, skills=[], langfuse_service=None,

    )

    names = [m["name"] for m in registry._all_skills_meta]
    assert "bocha_search" not in names
    assert "mineru" in names


def test_build_request_components_is_gone():
    """旧的合并方法必须删除，避免两条并存的装配路径。"""
    assert not hasattr(OrchestratorService, "_build_request_components")

```

*   [ ] **步骤 2：跑测试确认失败**
    

运行：`.venv/bin/python -m pytest tests/test_orchestrator_component_split.py -v` 预期：FAIL，`AttributeError: 'OrchestratorService' object has no attribute '_load_config_bundle'`

如果 `test_build_intent_components_wires_prompts` 因属性名不符而失败，先用 `.venv/bin/python -c "import inspect,app.intent.rewriter as m; print(inspect.getsource(m.QueryRewriter.__init__))"` 与 `app.intent.recognizer` 同法确认真实属性名，再按实际名字修正测试断言——**改测试断言，不改实现**。（2026-08-20 已核实 `_rewrite_prompt` / `_recognition_prompt` / `_orchestration_prompt` / `_default_orchestration` 四个名字为真，正常情况下不需要这一步。）

*   [ ] **步骤 3：用三个新方法替换** 
    

删除 `app/services/orchestrator_service.py` 中整个 `_build_request_components` 方法（从 `async def _build_request_components(` 到 `return registry, agent_factory, rewriter, recognizer`），在原位置插入：

```python
    async def _load_config_bundle(self, user_id: str, redis_client) -> dict:
        """读取登录时缓存的融合配置（步骤 1-4 的产物），未命中则 base-only 兜底。

        缓存命中 → 直接用登录时融合好的 merged_intents/agents/skills；
        缓存未命中 → base-only 兜底融合（不请求 mng，无外部意图），
        外部意图在下次登录后恢复。会话路径永不发起 mng HTTP 调用。
        """
        fused = await self._load_cached_user_config(user_id, redis_client)
        if fused is not None:
            return {

                "merged_intents": fused.get("merged_intents", []),


                "merged_agents": fused.get("merged_agents", []),


                "merged_skills": fused.get("merged_skills", []),

                "default_orchestration": fused.get("default_orchestration", {}),
            }
        logger.warning(
            f"[OrchestratorService] 用户配置缓存未命中 user={user_id}，"
            f"走 base-only 兜底（无外部意图），下次登录后恢复"
        )
        return await self._fuse_user_config(jwt_token="", permissions={})

    def _build_intent_components(self, fused: dict) -> tuple:
        """构建请求级改写器与识别器（纯内存，不依赖工作区）。

        Returns:
            (rewriter, recognizer)
        """
        intent_configs = [IntentConfig(**item) for item in fused["merged_intents"]]

        recognizer = IntentRecognizer(
            client=self._intent_client,
            model_config=self._intent_model_cfg,
            recognition_prompt=self._prompts.get("intent_recognition", ""),
            orchestration_prompt=self._prompts.get("intent_orchestration", ""),
            intent_configs=intent_configs,
            default_orchestration=fused["default_orchestration"],
        )
        rewriter = QueryRewriter(
            client=self._intent_client,
            model_config=self._intent_model_cfg,
            rewrite_prompt=self._prompts.get("rewrite", ""),
        )
        return rewriter, recognizer

    async def _prepare_workspace_components(
        self,
        fused: dict,
        user_id_safe: str,
        session_id_safe: str,
        search_enabled: bool = True,
        skills: Optional[List[str]] = None,
        langfuse_service: Optional[Any] = None,
    ) -> tuple:
        """获取/创建工作区并组装注册表与工厂（依赖沙箱，可与意图链路并行）。

        含 workspace-load 环节埋点。本方法会被 run() 放进 asyncio.create_task，
        因此不得读写 self 上的请求级状态（_last_agent_ids / _last_success 等）。

        Returns:
            (registry, agent_factory)
        """
        merged_agents = fused["merged_agents"]
        merged_skills = fused["merged_skills"]
        all_skill_dirs = [s["directory"] for s in merged_skills]

        # 环节埋点：工作区获取/创建子 span
        ws_ctx = (
            langfuse_service.start_span(
                "workspace-load",
                input={"user_id": user_id_safe, "session_id": session_id_safe},
            )
            if langfuse_service
            else _noop_ctx()
        )
        with ws_ctx as ws_span:
            workspace = await self._workspace_manager.get_workspace(user_id_safe, session_id_safe)
            if workspace is None:
                # 首次创建：create_workspace 内部会在 ws.initialize() 处单独记录 workspace-initialize 子 span
                workspace = await self._workspace_manager.create_workspace(
                    user_id=user_id_safe,
                    session_id=session_id_safe,
                    skill_dirs=all_skill_dirs,
                    langfuse_service=langfuse_service,
                )
            if ws_span:
                try:
                    ws_span.update(output={
                        "workspace_id": getattr(workspace, "workspace_id", None),
                    })
                except Exception:
                    pass

        from tools.chart_tools import (
            render_bar_chart, render_line_chart, render_pie_chart,
            render_generic_card, render_metric_card, render_confirm_action,
            render_indicator_table, render_selectable_list,
        )
        from agentscope.tool import FunctionTool
        from tools.mineru_tools import mineru_parse_tool

        # 工具层：根据后端选择 agentscope 原生工具 / OpenSandbox 桥接工具
        _chart_tools = [
            FunctionTool(render_pie_chart), FunctionTool(render_bar_chart),
            FunctionTool(render_line_chart), FunctionTool(render_generic_card),
            FunctionTool(render_metric_card), FunctionTool(render_confirm_action),
            FunctionTool(render_indicator_table), FunctionTool(render_selectable_list),
        ]
        if WORKSPACE_BACKEND == "opensandbox":
            from app.services.opensandbox_adapter import OpenSandboxToolAdapter
            from app.services.opensandbox_tool_bridge import create_opensandbox_tools
            # workspace 此处是 OpenSandbox Sandbox 实例
            adapter = OpenSandboxToolAdapter(
                workspace, workdir=f"/data/workspaces/{session_id_safe}"
            )
            all_tools = create_opensandbox_tools(adapter) + _chart_tools + [mineru_parse_tool]
            # 技能列表由管理器扫描沙箱内 /workspace/skills/ 获取
            all_skills_meta = await self._workspace_manager.list_skills(
                user_id=user_id_safe, session_id=session_id_safe
            )
        else:
            all_tools = [Bash(), Read(), Write(), Edit(), Glob(), Grep()] + _chart_tools + [mineru_parse_tool]
            all_skills_meta = await workspace.list_skills()

        # 按请求开关显隐联网搜索技能（workspace 始终装载全部技能，此处按轮次过滤）
        if not search_enabled:
            all_skills_meta = [
                m for m in all_skills_meta
                if (getattr(m, "name", None) or
                    (m.get("name") if isinstance(m, dict) else None)
                    ) != _SEARCH_SKILL_NAME
            ]

        agent_defs = [AgentDefinition(**a) for a in merged_agents]

        # 请求级附加技能：用户请求 skills ∪（search_enabled 时追加 bocha_search）
        # bocha_search 追加到 extra 后会 union 到每个 agent；
        # search_enabled=False 时 all_skills_meta 已移除 bocha_search，
        # extra 中的声明匹配不到 loader 自动失效，行为不变

        extra_skills = list(skills or [])

        if search_enabled:
            extra_skills.append(_SEARCH_SKILL_NAME)

        registry = AgentRegistry(
            definitions=agent_defs,
            workspace=workspace,
            all_tools=all_tools,
            all_skills_meta=all_skills_meta,
            create_model_fn=self._create_model_fn,
            extra_skill_names=extra_skills,
        )
        agent_factory = AgentFactory(registry)
        return registry, agent_factory

```

*   [ ] **步骤 4：让** 
    

把 `run()` 中的这段：

```python
        try:
            registry, agent_factory, rewriter, recognizer = (
                await self._build_request_components(
                    user_id=user_id,
                    redis_client=redis_client,
                    session_id=session_id,
                    search_enabled=search_enabled,
                    langfuse_service=langfuse_service,

                    skills=skills or [],

                )
            )
        except Exception as e:

```

替换为：

```python
        user_id_safe = user_id or "anonymous"
        session_id_safe = session_id or f"ephemeral-{user_id_safe}"
        try:
            fused = await self._load_config_bundle(user_id, redis_client)
            rewriter, recognizer = self._build_intent_components(fused)
            registry, agent_factory = await self._prepare_workspace_components(
                fused=fused,
                user_id_safe=user_id_safe,
                session_id_safe=session_id_safe,
                search_enabled=search_enabled,

                skills=skills or [],

                langfuse_service=langfuse_service,
            )
        except Exception as e:

```

`except` 分支内的三行（log / `_last_success = False` / yield error 事件 / return）保持原样不动。

*   [ ] **步骤 5：跑测试确认通过**
    

运行：`.venv/bin/python -m pytest tests/test_orchestrator_component_split.py -v` 预期：全部 PASS

*   [ ] **步骤 6：交付校验**
    

运行：`.venv/bin/python -m pytest` 预期：全绿（`test_degraded_and_parallel.py` 等既有编排测试必须仍绿，这是"纯重构未改行为"的证据）。**不要提交。**

---

## 任务 5：工作区准备与意图链路并行

**文件：**

*   修改：`app/services/orchestrator_service.py`（import 段加 `asyncio`；新增 `_resolve_workspace_task`；改 `run()`）
    
*   测试：`tests/test_orchestrator_parallel_workspace.py`（创建）
    

**接口：**

*   消费：任务 4 的 `_load_config_bundle` / `_build_intent_components` / `_prepare_workspace_components`
    
*   产出：`async _resolve_workspace_task(ws_task) -> tuple`，返回 `(registry, agent_factory, error_message)`；失败时前两项为 `None`、第三项为 `f"环境准备失败: {e}"` 且已把 `self._last_success` 置 `False`
    
*   [ ] **步骤 1：写失败的测试**
    

创建 `tests/test_orchestrator_parallel_workspace.py`：

```python
"""工作区准备与意图链路并行执行的时序、失败语义与 task 清理（纯 mock）。

不起真实沙箱与 LLM：把 _load_config_bundle / _build_intent_components /
_prepare_workspace_components 全部替换为可控桩，只验证 run() 的编排逻辑。
"""
import asyncio
import json

import pytest

from app.services.orchestrator_service import OrchestratorService
from app.intent.models import Intent

MESSAGES = [{"role": "user", "content": "帮我查一下今天的新闻"}]


def _events(chunks: list[str]) -> list[dict]:
    """把 SSE 字符串解析成 dict 列表，便于按 type 断言。"""

    out = [ ]

    for chunk in chunks:
        payload = chunk[len("data: "):].strip()
        out.append(json.loads(payload))
    return out


# 意图链路的桩必须真的耗时，否则"并行"与"串行"在总耗时上无法区分，
# 时序断言就成了永真断言。两段各 0.2s，合计 0.4s。
_INTENT_STEP_DELAY = 0.2


class _FakeRewriter:
    def __init__(self, marks: list):
        self._marks = marks

    async def rewrite(self, user_input, history):
        self._marks.append(("rewrite_start", asyncio.get_running_loop().time()))
        await asyncio.sleep(_INTENT_STEP_DELAY)
        return user_input


class _FakeRecognizer:
    def __init__(self, marks: list):
        self._marks = marks

    async def recognize_intents(self, query, history):
        self._marks.append(("recognize", asyncio.get_running_loop().time()))
        await asyncio.sleep(_INTENT_STEP_DELAY)
        return [Intent(id="general_chat", query=query, agent="general_agent")]

    async def plan_orchestration(self, query, intents):

        return "independent", [ ]


    def get_orchestration_mode(self, intent_result):
        return "parallel"


class _FakeOrchestrator:
    def __init__(self):

        self._last_results = [ ]


    async def run(self, intent_result, session_id=None, agent_states=None, langfuse_service=None):
        yield 'data: {"type": "fake_agent_done"}\n\n'


class _FakeRegistry:
    def get_definition(self, agent_id):
        return object()


def _make_service(marks: list, ws_delay: float = 0.3, ws_error: Exception | None = None):
    """构造只保留 run() 编排逻辑的服务实例，三个装配方法全部打桩。"""
    svc = OrchestratorService.__new__(OrchestratorService)
    svc._last_orchestrator = None

    svc._last_agent_ids = [ ]

    svc._last_success = True
    svc._orchestrator_params = {
        "parallel_timeout": 60, "pipeline_step_timeout": 60, "react_max_steps": 8,
    }

    async def _fake_bundle(user_id, redis_client):
        return {

            "merged_intents": [], "merged_agents": [],


            "merged_skills": [], "default_orchestration": {},

        }

    def _fake_intent_components(fused):
        return _FakeRewriter(marks), _FakeRecognizer(marks)

    async def _fake_workspace(**kwargs):
        await asyncio.sleep(ws_delay)
        marks.append(("workspace_done", asyncio.get_running_loop().time()))
        if ws_error is not None:
            raise ws_error
        return _FakeRegistry(), object()

    svc._load_config_bundle = _fake_bundle
    svc._build_intent_components = _fake_intent_components
    svc._prepare_workspace_components = _fake_workspace
    svc._create_orchestrator = lambda mode, agent_factory: _FakeOrchestrator()
    return svc


async def test_rewrite_starts_before_workspace_ready():
    """改写必须在工作区准备完成之前就开始——这是并行生效的判据。"""

    marks: list = [ ]

    svc = _make_service(marks, ws_delay=0.3)

    chunks = [c async for c in svc.run(MESSAGES)]

    names = [m[0] for m in marks]
    assert names.index("rewrite_start") < names.index("workspace_done")
    assert any(e["type"] == "fake_agent_done" for e in _events(chunks))


async def test_total_time_less_than_serial_sum():
    """总耗时应接近 max(工作区, 意图链路) 而非两者之和。

    工作区 0.3s、意图链路 0.4s：串行下限 0.7s，并行上限约 0.4s。
    阈值取 0.6s——高于并行值、低于串行值，改动前必失败、改动后必通过。
    """

    marks: list = [ ]

    svc = _make_service(marks, ws_delay=0.3)
    loop = asyncio.get_running_loop()

    t0 = loop.time()
    [c async for c in svc.run(MESSAGES)]
    elapsed = loop.time() - t0

    assert elapsed < 0.6


async def test_workspace_failure_yields_error_after_intent_events():
    """工作区失败 → 意图事件先出、error 事件后出，且不向上抛异常。"""

    marks: list = [ ]

    svc = _make_service(marks, ws_delay=0.05, ws_error=RuntimeError("sandbox unreachable"))

    events = _events([c async for c in svc.run(MESSAGES)])

    types = [e["type"] for e in events]
    assert "query_rewritten" in types
    assert "intents_recognized" in types
    assert types[-1] == "error"
    assert events[-1]["message"] == "环境准备失败: sandbox unreachable"
    assert types.index("query_rewritten") < types.index("error")
    assert svc._last_success is False


async def test_generator_closed_early_cancels_workspace_task():
    """客户端断连（生成器提前关闭）→ 工作区 task 被取消，不留悬挂 task。"""

    marks: list = [ ]

    svc = _make_service(marks, ws_delay=5.0)

    gen = svc.run(MESSAGES)
    first = await gen.__anext__()
    await gen.aclose()
    await asyncio.sleep(0)

    assert first.startswith("data: ")
    pending = [
        t for t in asyncio.all_tasks()
        if t.get_name() == "workspace-prepare" and not t.done()
    ]

    assert pending == [ ]

    assert ("workspace_done", ) not in [(m[0],) for m in marks]


async def test_agent_id_path_awaits_workspace_immediately():
    """agent_id 直连分支必须先拿到 registry 再走单 agent 路径。"""

    marks: list = [ ]

    svc = _make_service(marks, ws_delay=0.05)

    consumed: list = [ ]


    async def _fake_single(*args, **kwargs):
        consumed.append(args[0])
        yield 'data: {"type": "direct_done"}\n\n'

    svc._run_single_agent_path = _fake_single

    events = _events([c async for c in svc.run(MESSAGES, agent_id="general_agent")])

    assert [e["type"] for e in events] == ["direct_done"]
    assert isinstance(consumed[0], _FakeRegistry)


async def test_workspace_failure_on_agent_id_path():
    """agent_id 分支的工作区失败同样产出 error 事件、不抛。"""

    marks: list = [ ]

    svc = _make_service(marks, ws_delay=0.01, ws_error=RuntimeError("boom"))

    events = _events([c async for c in svc.run(MESSAGES, agent_id="general_agent")])

    assert events[-1]["type"] == "error"
    assert events[-1]["message"] == "环境准备失败: boom"

```

*   [ ] **步骤 2：跑测试确认失败**
    

运行：`.venv/bin/python -m pytest tests/test_orchestrator_parallel_workspace.py -v` 预期：FAIL。`test_rewrite_starts_before_workspace_ready` 报 `assert 1 < 0`（当前 workspace 先完成）；`test_total_time_less_than_serial_sum` 报 `assert 0.7x < 0.6`（串行耗时是两段之和）

*   [ ] **步骤 3：引入 asyncio 与** 
    

`app/services/orchestrator_service.py` 的 import 段，把 `import json` 上面一行改成两行（保持字母序）：

```python
import asyncio
import json

```

在 `_load_config_bundle` 方法之前插入：

```python
    async def _resolve_workspace_task(self, ws_task: "asyncio.Task") -> tuple:
        """等待工作区准备任务完成。

        失败不向调用方抛：把异常转成给前端的错误文案，由调用方 yield error 事件。
        文案与串行版本保持一致，避免前端出现新的错误形态。

        Returns:
            (registry, agent_factory, error_message)；失败时前两项为 None。
        """
        try:
            registry, agent_factory = await ws_task
            return registry, agent_factory, None
        except Exception as e:
            logger.exception("[OrchestratorService] 工作区准备失败")
            self._last_success = False
            return None, None, f"环境准备失败: {str(e)}"

```

*   [ ] **步骤 4：改** 
    

把任务 4 步骤 4 写下的那段（`user_id_safe = ...` 到 `except Exception as e:` 及其分支）整体替换为：

```python
        user_id_safe = user_id or "anonymous"
        session_id_safe = session_id or f"ephemeral-{user_id_safe}"
        try:
            fused = await self._load_config_bundle(user_id, redis_client)
            rewriter, recognizer = self._build_intent_components(fused)
        except Exception as e:
            # 配置装配失败不能让 SSE 流以 ASGI 异常中断，转为 error 事件
            logger.exception("[OrchestratorService] 请求级配置装配失败")
            self._last_success = False
            yield self._event({
                "type": "error",
                "message": f"环境准备失败: {str(e)}",
            })
            return

        # 工作区准备与意图链路并行：workspace 直到编排执行阶段才被消费，
        # 这段时间足以覆盖改写 + 意图识别 + 意图编排。
        ws_task = asyncio.create_task(
            self._prepare_workspace_components(
                fused=fused,
                user_id_safe=user_id_safe,
                session_id_safe=session_id_safe,
                search_enabled=search_enabled,

                skills=skills or [],

                langfuse_service=langfuse_service,
            ),
            name="workspace-prepare",
        )
        try:
            async for event_str in self._run_with_workspace_task(
                ws_task=ws_task,
                rewriter=rewriter,
                recognizer=recognizer,
                user_input=user_input,
                history=history,
                messages=messages,
                session_id=session_id,
                user_id=user_id,
                session_service=session_service,
                agent_id=agent_id,
                langfuse_service=langfuse_service,
            ):
                yield event_str
        finally:
            # 客户端断连会让本生成器被提前关闭：未完成则取消，已完成则消费异常，
            # 否则事件循环会报 "Task exception was never retrieved"
            if not ws_task.done():
                ws_task.cancel()
            elif not ws_task.cancelled():
                ws_task.exception()

```

`run()` 中原先从 `# ② 单 agent 短路路径` 直到方法末尾的全部内容，移入新方法 `_run_with_workspace_task`。在 `run()` 之后新增该方法，签名与主体如下（除标注处外，主体与原 `run()` 尾部逐行一致）：

```python
    async def _run_with_workspace_task(
        self,
        ws_task: "asyncio.Task",
        rewriter: QueryRewriter,
        recognizer: IntentRecognizer,
        user_input: str,
        history: List[dict],
        messages: List[Dict[str, Any]],
        session_id: Optional[str],
        user_id: Optional[str],
        session_service: Optional[Any],
        agent_id: Optional[str],
        langfuse_service: Optional[Any],
    ) -> AsyncGenerator[str, None]:
        """在等待工作区 task 的同时跑完意图链路，再执行编排。

        拆成独立方法是为了让 run() 的 finally 能无条件覆盖本方法的全部提前返回路径
        （return / 异常 / 生成器被关闭），保证 ws_task 不会悬挂。
        """
        # ② 单 agent 短路路径（跳过改写→识别→编排，直接由指定 agent 回答）
        if agent_id:
            registry, agent_factory, ws_err = await self._resolve_workspace_task(ws_task)
            if ws_err:
                yield self._event({"type": "error", "message": ws_err})
                return
            async for ev in self._run_single_agent_path(
                registry, agent_factory, agent_id, user_input,
                session_id, user_id, session_service, langfuse_service,
            ):
                yield ev
            return

```

其后紧接原 `run()` 的 `# ③ 查询改写` 到 `# ⑤ 意图编排` 结束（即 `yield self._event({"type": "intents_recognized", ...})` 那一段）全部原样保留，然后把 `# ⑥ 选择编排器并加载各 agent 状态` 之前插入 await：

```python
        # 编排前汇合：此时意图链路已跑完，工作区大概率已就绪
        registry, agent_factory, ws_err = await self._resolve_workspace_task(ws_task)
        if ws_err:
            yield self._event({"type": "error", "message": ws_err})
            return

        # ⑥ 选择编排器并加载各 agent 状态
        mode = recognizer.get_orchestration_mode(intent_result)
        orchestrator = self._create_orchestrator(mode, agent_factory)

```

从 `agent_states: Dict[str, AgentState] = {}` 到方法末尾的持久化逻辑全部原样保留，一行不改。

*   [ ] **步骤 5：跑测试确认通过**
    

运行：`.venv/bin/python -m pytest tests/test_orchestrator_parallel_workspace.py -v` 预期：全部 PASS

*   [ ] **步骤 6：回归既有编排测试**
    

运行：`.venv/bin/python -m pytest tests/test_degraded_and_parallel.py tests/test_pipeline_span.py tests/test_react_step_span.py tests/test_ttft.py -v` 预期：全绿

*   [ ] **步骤 7：交付校验**
    

运行：`.venv/bin/python -m pytest` 预期：全绿。**不要提交。**