# agents 包 — LangChain Agent 调度层
"""包入口：**延迟**再导出（PEP 562）。

⚠ 别改回"模块级 re-export"（曾经就是这么写的，CI 实测把它打回了）：

```python
from agents.base_agent import create_llm, create_memory   # 旧写法
from agents.master_agent import MasterAgent
```

那两行会让 `import agents.<任何子模块>` 都把 **master_agent 整条重依赖链**
（langchain / langgraph / langchain_openai / torch 链）拉起来，
CI 的"最小依赖"单测 job 里 10 个测试文件因此直接导入失败（`No module named 'langchain_openai'`）。

现在改成本模块 `__getattr__` 按需导入：`from agents import MasterAgent` 仍然可用，
但 `import agents.llm_metrics` / `from agents.data_quality import ...` 这类
**只用到哪个子模块就只加载哪个**。
"""
from typing import Any

_LAZY = {
    "create_llm": ("agents.base_agent", "create_llm"),
    "create_memory": ("agents.base_agent", "create_memory"),
    "MasterAgent": ("agents.master_agent", "MasterAgent"),
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'agents' has no attribute '{name}'")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value        # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
