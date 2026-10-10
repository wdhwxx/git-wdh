# 文件名：tests/leakage_gate/test_registry_contract.py
"""
test_registry_contract.py - 注册强制契约路径（M0-10 缺口补齐）
====================================================================
覆盖（M0-04 注册强制 + DEC-01 主键=name + M0-03 yaml 真源对齐）：
  - 全部已注册策略契约元数据齐全（_CONTRACT_REQUIRED_FIELDS 7 字段非空）
  - 注册表主键 = 策略类 name（DEC-01）
  - 缺契约字段的策略注册 → AttributeError（FakeNoContract 反向测试）
  - 注册表 name 集合 == config/strategy_registry.yaml 真源 name 集合（M0-03/M0-04 对齐）

防未来函数：测试只校验已注册类与已落盘注册表，不预测未来。
"""

from pathlib import Path
from typing import Any, Dict

import pytest

from myquant.strategies.base import StrategyBase
from myquant.strategies.registry import (
    _CONTRACT_REQUIRED_FIELDS,
    _strategy_registry,
    _validate_contract_metadata,
    register_strategy,
)

_REGISTRY_YAML: Path = Path("config/strategy_registry.yaml")


class _FakeNoContract(StrategyBase):
    """无契约元数据的策略（M0-04 反向测试用）。"""

    name = "fake_no_contract"

    def generate_signal(self, data: Any) -> Any:  # noqa: ANN401
        return data


# ═══════════════════════════════════════════════
#  注册强制契约（M0-04）
# ═══════════════════════════════════════════════

def test_registered_strategies_have_full_contract() -> None:
    """全部已注册策略契约元数据齐全（7 必填字段非空）。"""
    assert len(_strategy_registry) >= 7, f"注册表策略数异常: {len(_strategy_registry)}"
    for name, cls in _strategy_registry.items():
        for field in _CONTRACT_REQUIRED_FIELDS:
            value = getattr(cls, field, None)
            assert value, f"策略 {name} 缺契约字段 {field!r}（注册强制 M0-04）"


def test_registry_keyed_by_name() -> None:
    """注册表主键 = 策略类 name（DEC-01）。"""
    for name, cls in _strategy_registry.items():
        assert cls.name == name, f"注册表键 {name!r} 与策略类 name {cls.name!r} 不一致"


def test_validate_contract_metadata_missing_field_raises() -> None:
    """缺契约字段的类直接调 _validate_contract_metadata 抛 AttributeError。"""
    with pytest.raises(AttributeError) as exc_info:
        _validate_contract_metadata(_FakeNoContract)
    assert "_FakeNoContract" in str(exc_info.value)
    assert "契约元数据" in str(exc_info.value)


def test_register_without_contract_raises() -> None:
    """未带契约元数据的策略注册即报错（M0-04 验收：未带契约元数据的策略注册即报错）。"""
    with pytest.raises(AttributeError):
        register_strategy(_FakeNoContract)
    assert "fake_no_contract" not in _strategy_registry, "缺契约策略不得写入注册表"


def test_partial_contract_still_rejected() -> None:
    """契约字段部分缺失（仅填 name）仍被拒——完整性是 7 字段全有。"""

    class _PartialContract(StrategyBase):
        name = "fake_partial"
        decision_time = "bar_close"
        # 其余 6 字段缺失

        def generate_signal(self, data: Any) -> Any:  # noqa: ANN401
            return data

    with pytest.raises(AttributeError) as exc_info:
        register_strategy(_PartialContract)
    assert "decision_time" not in str(exc_info.value), "decision_time 已填不应报缺"
    assert "execution_time" in str(exc_info.value)


# ═══════════════════════════════════════════════
#  注册表与 yaml 真源对齐（M0-03/M0-04）
# ═══════════════════════════════════════════════

def test_registry_names_match_yaml_truth_source() -> None:
    """注册表 name 集合 == strategy_registry.yaml 真源 name 集合。"""
    assert _REGISTRY_YAML.is_file(), f"契约真源缺失: {_REGISTRY_YAML}"
    import yaml

    loaded: Dict[str, Any] = yaml.safe_load(_REGISTRY_YAML.read_text(encoding="utf-8"))
    yaml_names = {str(item["name"]) for item in loaded["strategies"]}
    registry_names = set(_strategy_registry.keys())
    assert yaml_names == registry_names, (
        f"注册表与 yaml 真源不一致: yaml 独有 {yaml_names - registry_names}，"
        f"注册表独有 {registry_names - yaml_names}"
    )
