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
        self.calls: list[str] = []
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


# ---- Task 3：list_skills 单命令化 + 缓存 + 跨用户兜底收口 ----
# 说明：返回值保持 list[Skill]（Toolkit.skills_or_loaders 仅接受 Skill/str，
# 不接受 dict），缓存层存 dict 元信息，返回时转换为 Skill 对象。

_SKILLS_STDOUT = "bocha_search\t---\nmineru\t# MinerU 文档解析技能\n"


def test_list_skills_uses_single_command():
    """扫描技能只发一条命令，不再是 1 次 ls + 每技能 1 次 head。"""
    sbx = _FakeSandbox(stdout_map={"/workspace/skills": _SKILLS_STDOUT})
    mgr = _make_manager()
    _seed(mgr, sbx)

    skills = asyncio.run(mgr.list_skills(user_id="u1", session_id="s1"))

    assert len(sbx.calls) == 1
    assert [s.name for s in skills] == ["bocha_search", "mineru"]
    assert skills[1].dir == "/workspace/skills/mineru"
    assert skills[1].description == "# MinerU 文档解析技能"


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
    # 缓存的是 dict 元信息，返回值是由它构建的 Skill 对象
    assert [m["name"] for m in entry.skills_meta] == [s.name for s in first]


def test_list_skills_exception_returns_empty_without_caching():
    """扫描失败返回 []，且不写缓存——下一轮还能重试。"""
    class _Boom(_FakeSandbox):
        async def run(self, cmd: str):
            raise RuntimeError("exec failed")

    mgr = _make_manager()
    entry = _seed(mgr, _Boom())

    assert asyncio.run(mgr.list_skills(user_id="u1")) == []

    assert entry.skills_meta is None


def test_list_skills_without_entry_returns_empty_not_other_user_sandbox():
    """无该用户的沙箱时返回 []，绝不回落到别人的沙箱（跨用户技能清单泄漏）。"""
    other = _FakeSandbox(stdout_map={"/workspace/skills": _SKILLS_STDOUT})
    mgr = _make_manager()
    _seed(mgr, other, user_id="someone_else")

    assert asyncio.run(mgr.list_skills(user_id="u1")) == []

    assert asyncio.run(mgr.list_skills(user_id="")) == []

    assert other.calls == []

