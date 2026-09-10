"""regulations 模型配置解析：resolve_regulations_model 专用模型优先、缺失/空字段回退 default。"""
import pytest

from app.regulations.config import resolve_regulations_model


_FULL_QA = {
    "models": {
        "regulations_qa": {
            "provider": "openai",
            "model_name": "regu-qa-model",
            "base_url": "http://regu-llm:8000/v1",
            "api_key": "sk-regu",
            "parameters": {"temperature": 0.2, "timeout": 60},
        },
        "default": {
            "provider": "openai",
            "model_name": "default-model",
            "base_url": "http://default-llm:8000/v1",
            "api_key": "sk-default",
            "parameters": {"temperature": 0.7},
        },
    }
}

_DEFAULT_ONLY = {"models": {"default": _FULL_QA["models"]["default"]}}


def test_resolve_model_uses_regulations_qa_when_complete():
    """regulations_qa 节完整（model_name/base_url/api_key 均非空）→ 用专用模型。"""
    resolved = resolve_regulations_model(_FULL_QA)
    assert resolved == {
        "provider": "openai",
        "model_name": "regu-qa-model",
        "base_url": "http://regu-llm:8000/v1",
        "api_key": "sk-regu",
        "parameters": {"temperature": 0.2, "timeout": 60},
    }


def test_resolve_model_falls_back_when_section_missing():
    """models.regulations_qa 整节缺失 → 回退 models.default。"""
    resolved = resolve_regulations_model(_DEFAULT_ONLY)
    assert resolved["model_name"] == "default-model"
    assert resolved["base_url"] == "http://default-llm:8000/v1"
    assert resolved["api_key"] == "sk-default"
    assert resolved["provider"] == "openai"


@pytest.mark.parametrize("field", ["model_name", "base_url", "api_key"])
def test_resolve_model_falls_back_when_field_empty(field):
    """regulations_qa 任一关键字段为空字符串 → 回退 default。"""
    section = dict(_FULL_QA["models"]["regulations_qa"])
    section[field] = ""
    cfg = {"models": {"regulations_qa": section, "default": _FULL_QA["models"]["default"]}}

    resolved = resolve_regulations_model(cfg)
    assert resolved["model_name"] == "default-model"


def test_resolve_model_falls_back_when_section_empty_dict():
    """regulations_qa 为空 dict（falsy）→ 回退 default。"""
    cfg = {
        "models": {
            "regulations_qa": {},
            "default": _FULL_QA["models"]["default"],
        }
    }
    resolved = resolve_regulations_model(cfg)
    assert resolved["model_name"] == "default-model"


@pytest.mark.parametrize("cfg", [None, {}, {"models": {}}])
def test_resolve_model_handles_missing_models(cfg):
    """model_config 为 None / 无 models 节 / models 为空 → 返回带默认值的空结构。"""
    resolved = resolve_regulations_model(cfg)
    assert resolved == {
        "provider": "openai",
        "model_name": "",
        "base_url": "",
        "api_key": "",
        "parameters": {},
    }


def test_resolve_model_default_missing_returns_empty_defaults():
    """regulations_qa 缺失且 default 也缺失 → 空 defaults（provider 兜底 openai）。"""
    resolved = resolve_regulations_model({"models": {"other": {"model_name": "x"}}})
    assert resolved["provider"] == "openai"
    assert resolved["model_name"] == ""
    assert resolved["parameters"] == {}


def test_resolve_model_defaults_provider_and_parameters():
    """节存在但 provider/parameters 缺省 → provider=openai、parameters={}。"""
    cfg = {
        "models": {
            "regulations_qa": {
                "model_name": "m",
                "base_url": "http://b",
                "api_key": "k",
            }
        }
    }
    resolved = resolve_regulations_model(cfg)
    assert resolved["provider"] == "openai"
    assert resolved["model_name"] == "m"
    assert resolved["parameters"] == {}


def test_resolve_model_parameters_none_becomes_empty_dict():
    """parameters 显式为 None → 归一为 {}。"""
    cfg = {
        "models": {
            "regulations_qa": {
                "model_name": "m",
                "base_url": "http://b",
                "api_key": "k",
                "parameters": None,
            }
        }
    }
    assert resolve_regulations_model(cfg)["parameters"] == {}
