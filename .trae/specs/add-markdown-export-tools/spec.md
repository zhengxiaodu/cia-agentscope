# 新增 Markdown 导出后端工具（docx/pdf/xlsx/png）Spec

## Why

用户需要智能体把 Markdown 内容导出为 Word/PDF/Excel/图片文件。上游 [bowenliang123/markdown-exporter](https://github.com/bowenliang123/markdown-exporter)（Apache-2.0，PyPI 包 `md-exporter` 4.0.0）已提供成熟的转换实现（docx/pdf/png 走 pandoc+typst，自带二进制与中文字体；xlsx 走 pandas+openpyxl），无需重复造轮子。本变更以其为依赖，在后端封装 4 个 agentscope FunctionTool 并注入 `all_tools`。

## What Changes

- **新增** `tools/md_export_tools.py`：工厂 `create_md_export_tools(read_file, write_file) -> list[FunctionTool]`，产出 `md_to_docx` / `md_to_pdf` / `md_to_xlsx` / `md_to_png` 4 个工具（闭包捕获工作区读写函数）。
- **改** `app/services/orchestrator_service.py`：在 opensandbox 与 docker 两个分支的 `all_tools` 组装处追加 4 个工具（opensandbox 用 adapter 闭包读写沙箱，docker 用 `WORKSPACE_BASEDIR/{session_id}` 本地读写）。
- **改** `requirements.txt`：追加 `md-exporter>=4.0.0`。
- **新增** `tests/test_md_export_tools.py`：参数校验、读写接线、转换调用（mock）与 xlsx 真实转换冒烟测试。

## Impact

- Affected specs: `detect-and-stream-new-files` / `persist-and-return-session-files`（输出文件落入会话工作区后自动进入 `files_generated` 快照差分检测、`session_files` 落库与 `/files/{session_id}/{path}` 下载链路，无需改动这两处代码）
- Affected code: `tools/md_export_tools.py`（新）、`app/services/orchestrator_service.py`（all_tools 两处）、`requirements.txt`、`tests/test_md_export_tools.py`（新）
- 外部依赖: `md-exporter>=4.0.0`（传递引入 markdown、pandas[excel,html,xml]、typst、pypandoc-binary，均带预编译 wheel，无需系统级安装 pandoc/typst）

## ADDED Requirements

### Requirement: Markdown 导出工具集

系统 SHALL 在每轮 `/chat` 的工具组装中提供 4 个后端 FunctionTool：`md_to_docx`、`md_to_pdf`、`md_to_xlsx`、`md_to_png`，将 Markdown 内容转换为对应格式文件并写入当前会话工作区。工具由 `tools/md_export_tools.py` 的工厂 `create_md_export_tools(read_file, write_file)` 创建，转换逻辑复用 `md_exporter` 包的 `convert_md_to_docx` / `convert_md_to_pdf` / `convert_md_to_xlsx` / `convert_md_to_png`（`md_exporter.services.svc_md_to_*` 模块）。

#### Scenario: 以文本为输入导出 docx
- **WHEN** 模型调用 `md_to_docx(markdown_text="# 标题\n正文")`
- **THEN** 后端在临时目录完成转换，把 .docx 字节写入会话工作区根目录（默认文件名 `export_YYYYMMDD_HHMMSS.docx` 或由 `output_file_name` 指定），工具返回创建的工作区相对路径

#### Scenario: 以工作区文件为输入导出 pdf
- **WHEN** 模型先用 Write 工具落盘 `report.md`，再调用 `md_to_pdf(markdown_file_path="report.md")`
- **THEN** 工具经注入的 `read_file` 读取该 .md 内容，转换后把 `report.pdf` 写入工作区，返回相对路径

#### Scenario: markdown 表格导出 xlsx
- **WHEN** 输入 Markdown 含合法表格语法
- **THEN** 产出 .xlsx（每个 Markdown 表格一个 sheet），写入工作区并返回路径

#### Scenario: markdown 导出 png（多页）
- **WHEN** 调用 `md_to_png(..., multi_page=True)` 且文档渲染为多页
- **THEN** 每页生成一个编号文件（`name_1.png`、`name_2.png`…），工具返回全部文件路径列表

#### Scenario: 本轮结束后文件进入既有返回链路
- **WHEN** 任一导出工具成功写入工作区
- **THEN** 该文件被既有 `files_generated` 快照差分检测捕获（opensandbox 走 `list_session_files`，docker 走本地 snapshot），元信息落 `session_files` 表，用户可经 `/files/{session_id}/{path}` 下载——本变更不修改该链路任何代码

#### Scenario: 参数二选一校验
- **WHEN** `markdown_text` 与 `markdown_file_path` 同时为空，或同时提供
- **THEN** 工具不执行转换，返回错误提示文本（ToolChunk），说明必须且只能提供其一

#### Scenario: 路径越权防护
- **WHEN** `markdown_file_path` 或 `output_file_name` 含绝对路径、`..`、或反斜杠转义
- **THEN** 拒绝执行，返回错误提示（与 `app/routes/files.py` 的越权校验规则一致）

#### Scenario: 转换失败降级
- **WHEN** `md_exporter` 未安装（ImportError）、输入不是合法 Markdown 表格（xlsx 场景）、或 pandoc/typst 转换抛错
- **THEN** 工具捕获异常，返回包含错误原因的 ToolChunk 文本，不中断 /chat 主流程

### Requirement: 工具装配（all_tools 注入）

`orchestrator_service` SHALL 在两个后端分支的工具组装中追加 4 个导出工具：
- opensandbox 分支：以 `OpenSandboxToolAdapter` 为基础构造二进制安全的 `read_file`/`write_file` 闭包（写入经 base64 + bash 落盘，参照 `opensandbox_workspace_manager` 既有 base64 读写模式；文本读取可直接用 `adapter.read`）
- docker 分支：以 `os.path.join(WORKSPACE_BASEDIR, session_id)` 为根目录的本地文件读写闭包（写入前 `os.makedirs(exist_ok=True)`）

#### Scenario: opensandbox 后端
- **WHEN** `WORKSPACE_BACKEND=opensandbox`
- **THEN** `all_tools = create_opensandbox_tools(adapter) + _chart_tools + [policy_qa_tool] + md_tools`，导出文件出现在沙箱 `/data/workspaces/{session_id}/` 下

#### Scenario: docker 后端
- **WHEN** `WORKSPACE_BACKEND=docker`
- **THEN** `all_tools = [Bash(), Read(), Write(), Edit(), Glob(), Grep()] + _chart_tools + [policy_qa_tool] + md_tools`，导出文件出现在宿主机 `{WORKSPACE_BASEDIR}/{session_id}/` 下

### Requirement: 异步与阻塞隔离

转换函数（pandoc/typst 子进程调用）为同步阻塞实现，工具执行体 SHALL 通过 `asyncio.to_thread` 在线程中运行转换，避免阻塞 FastAPI 事件循环。工作区读写（opensandbox 远程调用）保持 async。

## 假设与决策

1. **引入方式**：pip 依赖 `md-exporter>=4.0.0`（用户已确认），不复制源码进仓库。若沙箱/部署环境 PyPI 安装失败，回退 `pip install git+https://github.com/bowenliang123/markdown-exporter.git`。
2. **依赖隔离**：`tools/md_export_tools.py` 不在模块顶层 import `md_exporter`，转换函数在工具执行体内延迟 import——包缺失时工具返回友好错误，不影响后端启动与 /chat 主流程（对齐 orchestrator 对 tools.* 的函数级 import 惯例）。
3. **临时文件策略**：转换先写本地 `tempfile.TemporaryDirectory()`，成功后读取字节经 `write_file` 上传工作区，临时目录自动清理（上游 `convert_md_to_*` 均要求本地 `output_path: Path`）。
4. **输出位置**：统一写工作区根目录（相对路径即文件名），不做子目录参数——模型可用 output_file_name 控制命名；保持工具面最小。
5. **参数面**：每个工具仅 `markdown_text` / `markdown_file_path`（二选一）、`output_file_name`（可选）；`md_to_png` 额外 `multi_page: bool = False`。不透传 template_path / is_enable_toc 等上游高级参数。
6. **工具暴露范围**：`all_tools` 进入 `AgentRegistry._all_tools` 后，`_build_toolkit_for` 会把全部工具装配进每个 agent 的 Toolkit（现状即如此，技能才按绑定过滤），故无需改 agent 配置。
7. **PNG 上游能力**：`md_to_png` 在上游 4.0.0 已由 typst 重新支持（PR #179），非 2026-04 被移除的旧实现。
8. **md_to_png 多页命名**：沿用上游 `stem_1.png` 编号约定，多文件全部上传并在返回文本中列出。

## 验证

详见 checklist.md。
