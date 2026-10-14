"""P1-04 引擎直构例外清单门禁测试。

AST 扫描 ``scripts/`` 下所有 .py，找出绕过出口工厂的引擎直构点
（``LiveEngine(`` / ``BacktestEngine.from_data_provider``），断言每个命中点
都已在 ``REALISM_EXEMPT_ENTRIES`` 登记——新增未登记直构点即判红，
防"门禁绕过"通道静默敞口。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from myquant.backtest.exempt_registry import REALISM_EXEMPT_ENTRIES

#: 项目根（tests/unit 的上两级）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"

#: 需要核对的直构符号（Attribute/Call 节点的匹配子串）
_DIRECT_CONSTRUCT_SYMBOLS: tuple[str, ...] = (
    "LiveEngine",
    "BacktestEngine.from_data_provider",
)


def _iter_direct_constructs(script_path: Path) -> list[str]:
    """用 AST 找出脚本里所有引擎直构符号。

    Args:
        script_path: 脚本文件路径。

    Returns:
        命中的直构符号子串列表（未命中返回空）。
    """
    source = script_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(script_path))
    except SyntaxError:
        return []

    hits: list[str] = []
    for node in ast.walk(tree):
        # Call(func=Name('LiveEngine')) → 直构
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "LiveEngine":
                hits.append("LiveEngine(")
        # Call(func=Attribute(value=Name('BacktestEngine'), attr='from_data_provider'))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            qual = f"{node.func.value.id}.{node.func.attr}"
            if qual == "BacktestEngine.from_data_provider":
                hits.append("BacktestEngine.from_data_provider")
    return hits


def test_exempt_registry_is_not_empty() -> None:
    """例外清单非空（至少登记 3 个沙箱 LiveEngine 直构点）。"""
    assert len(REALISM_EXEMPT_ENTRIES) >= 3


@pytest.mark.parametrize("script_path", sorted(_SCRIPTS_DIR.glob("*.py")))
def test_every_direct_construct_is_exempted(script_path: Path) -> None:
    """每个 scripts/ 直构点都必须在 REALISM_EXEMPT_ENTRIES 登记。

    未登记 = 新增了绕过出口工厂的直构（门禁静默敞口），判红。
    """
    rel = script_path.relative_to(_PROJECT_ROOT).as_posix()
    hits = _iter_direct_constructs(script_path)
    if not hits:
        return  # 该脚本无直构点，通过

    registered = {e.script: e for e in REALISM_EXEMPT_ENTRIES}
    assert rel in registered, (
        f"{rel} 存在引擎直构点 {hits}，但未在 REALISM_EXEMPT_ENTRIES 登记"
        f"（P1-04：直构点必须收敛到 create_engine/create_live_engine 或显式豁免）"
    )
    entry = registered[rel]
    for h in set(hits):
        assert entry.symbol in h or h.startswith(entry.symbol.rstrip("(")), (
            f"{rel} 直构符号 {h!r} 与登记符号 {entry.symbol!r} 不匹配"
        )
