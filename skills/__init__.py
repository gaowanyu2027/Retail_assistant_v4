# skills 包 — 分析技能模块
"""包入口：**延迟**再导出（PEP 562）。

与 `agents/`、`cv_engine/` 同一原因：模块级 re-export 会让
`import skills.skill_new_modules`（纯业务逻辑测试用）连带把
skill_popularity / skill_anomaly / skill_emotion 全加载一遍，
其中的 cv_engine 依赖链在 CI 最小依赖环境里是不存在的。

`from skills import PopularitySkill` 仍照旧可用（走下面的 `__getattr__`）。
"""
from typing import Any

_LAZY = {
    "PopularitySkill": ("skills.skill_popularity", "PopularitySkill"),
    "AnomalySkill": ("skills.skill_anomaly", "AnomalySkill"),
    "AnomalyAlert": ("skills.skill_anomaly", "AnomalyAlert"),
    "SkillEmotion": ("skills.skill_emotion", "SkillEmotion"),
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'skills' has no attribute '{name}'")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
