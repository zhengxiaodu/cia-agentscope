"""pytest 公共配置。"""
import pytest

# pytest-asyncio 配置：自动为 async test 添加 event loop
pytest_plugins = ("pytest_asyncio",)


def pytest_collection_modifyitems(items):
    """为所有 async def test_ 自动标记 asyncio 标签。"""
    for item in items:
        if asyncio_marker_needed(item):
            item.add_marker(pytest.mark.asyncio)


def asyncio_marker_needed(item) -> bool:
    """判断一个 test item 是否需要 asyncio 标记。"""
    import inspect
    if not inspect.iscoroutinefunction(item.function):
        return False
    return not any(m.name == "asyncio" for m in item.own_markers)
