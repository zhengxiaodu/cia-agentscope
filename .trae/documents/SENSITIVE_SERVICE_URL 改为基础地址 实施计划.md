# SENSITIVE_SERVICE_URL 改为基础地址（IP+端口）实施计划

## 概要

`.env` 中 `SENSITIVE_SERVICE_URL` 不再带 `/sensitive/check` 路径，只配置服务基础地址（`http://IP:端口`）；由 `sensitive_service.py` 在调用时根据场景拼接具体接口路径：
- 输出审核 → `{base}/sensitive/check`
- 输入词典审核 → `{base}/sensitive/dict-check`

## 现状分析（Phase 1 探索结论）

- [.env](file:///workspace/.env#L91)：`SENSITIVE_SERVICE_URL=http://25.59.38.152:30017/sensitive/check`（带完整路径）
- [sensitive_service.py](file:///workspace/app/services/sensitive_service.py#L59-L71)：`_dict_check_url()` 通过后缀替换派生 dict-check 地址，不匹配后缀时记 warning 并返回空串（调用方兜底放行）；`check_sensitive` 直接 POST `SENSITIVE_SERVICE_URL`
- [config.py](file:///workspace/app/config.py#L139-L140)：仅透传环境变量，注释描述"敏感检测服务地址"
- [test_sensitive_service.py](file:///workspace/tests/test_sensitive_service.py)：`_install_httpx` monkeypatch 值为 `"http://fake/sensitive/check"`；L523 断言 dict-check 请求 URL；L551-558 测试"URL 不可派生"分支

## 具体改动

### 1. [.env](file:///workspace/.env#L91)

```ini
# 敏感检测服务地址（基础地址 IP+端口，路径由服务代码按场景拼接），留空则关闭检测
SENSITIVE_SERVICE_URL=http://25.59.38.152:30017
```

### 2. [app/config.py](file:///workspace/app/config.py#L139-L140)

仅更新注释：

```python
# 敏感检测服务基础地址（IP+端口；/sensitive/check 与 /sensitive/dict-check 由服务代码拼接），留空则关闭检测
SENSITIVE_SERVICE_URL = os.getenv("SENSITIVE_SERVICE_URL", "")
```

### 3. [app/services/sensitive_service.py](file:///workspace/app/services/sensitive_service.py)

**a) `_dict_check_url()` 替换为 `_service_base()`**：

```python
def _service_base() -> str:
    """返回敏感检测服务基础地址（IP+端口形式，去掉尾部斜杠）。

    兼容遗留的完整路径写法（…/sensitive/check 或 …/sensitive/dict-check）：
    剥掉旧后缀并告警，保证升级 .env 前后行为一致。
    """
    base = SENSITIVE_SERVICE_URL.rstrip("/")
    for suffix in ("/sensitive/check", "/sensitive/dict-check"):
        if base.endswith(suffix):
            logger.warning(
                "[sensitive_service] SENSITIVE_SERVICE_URL 建议只配置 IP+端口基础地址，"
                "已自动剥离遗留路径后缀 %s", suffix,
            )
            base = base[: -len(suffix)]
            break
    return base
```

**b) 两个入口在 `do_request` 内拼接路径**（保持调用时读取，确保测试 monkeypatch 生效）：
- `check_sensitive`：`url = f"{_service_base()}/sensitive/check"`，POST 该地址
- `dict_check_sensitive`：`url = f"{_service_base()}/sensitive/dict-check"`；删除原"URL 不可派生 → 兜底放行"分支（该分支随派生逻辑一并消失）

**c) 更新模块 docstring** 中关于地址的描述。

### 4. [tests/test_sensitive_service.py](file:///workspace/tests/test_sensitive_service.py)

- `_install_httpx`：monkeypatch 值改为 `"http://fake"`
- `test_check_sensitive_hit`：新增断言 `client.post_calls[0]["url"] == "http://fake/sensitive/check"`
- `test_dict_check_hit`：URL 断言 `http://fake/sensitive/dict-check` 保持有效，无需改
- `test_dict_check_url_not_derivable` 重写为**遗留路径兼容测试**：monkeypatch 为 `"http://fake/sensitive/check"`（旧配置格式），断言 dict-check 仍请求 `http://fake/sensitive/dict-check` 且正常命中
- 新增：`test_legacy_url_check_endpoint`——monkeypatch 为旧格式 `"http://fake/sensitive/dict-check"`，`check_sensitive` 应请求 `http://fake/sensitive/check`

## 假设与决策

1. **空地址短路不变**：`SENSITIVE_SERVICE_URL` 为空仍等效关闭检测（`_run_check` 中现有逻辑保留）。
2. **遗留格式兼容**：自动剥离 `/sensitive/check`、`/sensitive/dict-check` 后缀并告警，避免部署环境 .env 未同步导致服务失效；一个版本周期后可移除该兼容逻辑。
3. 请求体、响应解析、开关门控均不变——本计划仅改地址的组成方式。

## 验证步骤

1. `python -m pytest tests/test_sensitive_service.py -q`（更新后全绿）
2. 全量回归 `python -m pytest tests/ -q`（当前基线 166 passed）
3. grep 确认 `.env` 中地址不再含 `/sensitive/check` 路径
