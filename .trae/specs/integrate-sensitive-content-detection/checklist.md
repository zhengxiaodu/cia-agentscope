# Checklist

- [ ] `.env` 包含 `SENSITIVE_SERVICE_URL`、`SENSITIVE_THRESHOLD`、`SENSITIVE_TIMEOUT` 三个配置项
- [ ] `app/config.py` 读取上述三个环境变量，并提供默认值（URL 默认空、阈值默认 0.7、超时默认 5）
- [ ] `app/services/sensitive_service.py` 存在 `check_sensitive(text, stage)` 异步函数
- [ ] `check_sensitive` 在 `code==0 and hasSensitiveWord==True` 时返回 `blocked=True` 及 `reason`、`category`
- [ ] `check_sensitive` 在服务地址为空时跳过检测并返回 `blocked=False`
- [ ] `check_sensitive` 在网络异常/超时/HTTP 非 2xx/JSON 解析失败/`code!=0` 时记录 warning 日志并返回 `blocked=False`
- [ ] `check_sensitive` 每次调用通过 Langfuse span 记录输入文本、输出响应、耗时与 stage
- [ ] `app/routes/chat.py` 在 `session_ready` 之后、`generate_response` 之前对用户输入做敏感检测
- [ ] 用户输入命中时 yield `message_replace` 事件（含 `stage="input"` 与 `reason`）并结束流，不进入 `generate_response`
- [ ] 用户输入未命中时正常进入 `generate_response`
- [ ] `app/services/chat_service.py` 在编排流结束、`final_output` 计算完毕后、推荐问题生成前对 `final_output` 做敏感检测
- [ ] `final_output` 命中时 yield `message_replace` 事件（含 `stage="output"` 与 `reason`）并跳过推荐问题生成
- [ ] `final_output` 命中时持久化与文件检测步骤仍执行
- [ ] `message_replace` 事件结构符合 spec 定义（`type`/`message`/`reason`/`stage`）
- [ ] 单元测试覆盖命中、未命中、各类兜底场景
