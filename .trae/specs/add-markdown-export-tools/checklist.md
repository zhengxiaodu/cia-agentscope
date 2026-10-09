# Checklist

- [x] requirements.txt 含 `md-exporter>=4.0.0`，且沙箱内 pip 安装成功、4 个 `convert_md_to_*` 导入验证通过
- [x] `tools/md_export_tools.py` 提供 `create_md_export_tools(read_file, write_file)` 工厂，产出 md_to_docx / md_to_pdf / md_to_xlsx / md_to_png 四个 FunctionTool
- [x] 每个工具支持 `markdown_text` / `markdown_file_path` 二选一输入，`output_file_name` 可选；md_to_png 支持 `multi_page`
- [x] 双空 / 双传 / 路径越权（绝对路径、`..`）均被拒绝并返回错误 ToolChunk，不触发转换
- [x] 转换在 `asyncio.to_thread` + 临时目录中执行，产物字节经 `write_file` 写入会话工作区，工具返回相对路径（png 多页全量返回）
- [x] `md_exporter` 为延迟 import：包缺失时工具返回友好错误，后端启动与 /chat 不受影响
- [x] orchestrator_service 两个分支（opensandbox / docker）的 `all_tools` 均已追加 4 个工具
- [x] opensandbox 分支的写闭包为二进制安全（base64+bash 或 SDK 二进制写），可落盘 docx/pdf/xlsx/png 字节
- [x] docker 分支写闭包以 `WORKSPACE_BASEDIR/{session_id}` 为根且自动建目录
- [x] 导出文件被既有 `files_generated` 检测捕获并可经 `/files/{session_id}/{path}` 下载（不修改该链路代码）
- [x] tests/test_md_export_tools.py 覆盖参数校验、读写接线、png 多页、ImportError 降级、xlsx 真实转换冒烟（md-exporter 缺失时 skip）
- [x] 全量回归 `python -m pytest tests/ -q` 通过
