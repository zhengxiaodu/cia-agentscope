"""workspace_file_access 纯函数测试：list_skills 命令构造与输出解析。"""


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

    assert parse_skills_output("\n*\t\n  \n") == []


def test_parse_skills_output_keeps_tabs_inside_description():
    """描述里含 TAB 时只按第一个 TAB 切分，描述整体保留。"""
    from app.services.workspace_file_access import parse_skills_output

    out = parse_skills_output("policy_qa\tdesc\twith\ttab\n")

    assert out[0]["description"] == "desc\twith\ttab"
