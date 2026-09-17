"""工作区文件访问的纯函数集合：沙箱命令构造与输出解析。

纯函数不碰沙箱、不持有状态，可独立单测；调用方为
OpenSandboxWorkspaceManager（把命令发给沙箱、把 stdout 解析回结构）。
"""
from typing import List

# 沙箱内技能根目录（技能在沙箱创建时注入，生命周期内不变）
SKILLS_ROOT = "/workspace/skills"


def build_list_skills_command(skills_root: str = SKILLS_ROOT) -> str:
    """构造一次性列出全部技能名与描述首行的命令（单次沙箱往返）。

    输出每行 `名称<TAB>描述首行`。`[ -d "$d" ]` 用于过滤 glob 未匹配时残留的
    字面量路径；`head -1` 与旧实现的 `head -5` 取首行等价。
    """
    return (
        f'for d in {skills_root}/*/; do [ -d "$d" ] || continue; '
        'n=$(basename "$d"); h=$(head -1 "$d/SKILL.md" 2>/dev/null); '
        "printf '%s\t%s\\n' \"$n\" \"$h\"; done"
    )


def parse_skills_output(stdout: str, skills_root: str = SKILLS_ROOT) -> List[dict]:
    """解析 build_list_skills_command 的输出为技能元信息列表。

    - 只按第一个 TAB 切分，描述内的 TAB 原样保留
    - 描述为空时回落为技能名（与旧实现一致）
    - 跳过空行与 glob 字面量 '*'
    """
    skills: List[dict] = []

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
