# Tasks

- [x] Task 1: 添加依赖并验证可安装
  - [x] requirements.txt 追加 `md-exporter>=4.0.0`
  - [x] 沙箱 `pip install md-exporter` 成功；验证 `from md_exporter.services.svc_md_to_docx import convert_md_to_docx` 等 4 个导入可用
  - [x] 失败则回退 git 安装方式并记录（未触发，PyPI 安装成功）

- [x] Task 2: 实现 tools/md_export_tools.py
  - [x] 工厂 `create_md_export_tools(read_file, write_file)`，read_file: async (rel_path) -> str，write_file: async (rel_path, bytes) -> None
  - [x] 4 个闭包工具 `md_to_docx` / `md_to_pdf` / `md_to_xlsx` / `md_to_png`，中文 docstring（说明输入二选一、输出位置）
  - [x] 参数校验：text/path 二选一；output_file_name / markdown_file_path 越权检查（禁绝对路径与 `..`）
  - [x] 转换经 `asyncio.to_thread` 在 `tempfile.TemporaryDirectory()` 中执行；`md_exporter` 延迟 import，ImportError 返回友好错误
  - [x] 成功后经 write_file 上传工作区，返回创建的相对路径文本（png 多页全部上传并列出）
  - [x] 默认输出名 `export_YYYYMMDD_HHMMSS.<ext>`；xlsx 扩展名 .xlsx 等

- [x] Task 3: orchestrator_service 装配
  - [x] opensandbox 分支：构造二进制安全读写闭包（文本读 `adapter.read`；二进制写经 base64+bash，参照 opensandbox_workspace_manager 既有模式），`all_tools` 追加 `create_md_export_tools(...)`
  - [x] docker 分支：`WORKSPACE_BASEDIR/{session_id}` 本地读写闭包（makedirs exist_ok），`all_tools` 追加
  - [x] 保持函数级 import 惯例

- [x] Task 4: 测试 tests/test_md_export_tools.py
  - [x] 参数校验用例：双空/双传/路径越权（mock 读写，断言不调用转换）
  - [x] 接线用例：monkeypatch 4 个转换函数，断言 read_file 被调、write_file 收到转换产物字节、返回文本含路径
  - [x] png 多页用例：mock 返回多文件，断言全部上传
  - [x] ImportError 降级用例（monkeypatch import 触发失败）
  - [x] xlsx 真实转换冒烟：markdown 表格 -> .xlsx 字节（PK zip 魔数），md-exporter 不可导入时 skip
  - [x] 全量回归 `python -m pytest tests/ -q`（202 passed）

# Task Dependencies
- Task 2 依赖 Task 1（需确认导入路径后写延迟 import）
- Task 3 依赖 Task 2（工厂签名确定后装配）
- Task 4 依赖 Task 2/3
