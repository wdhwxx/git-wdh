"""P0-08 子项④（止血 C）反红测试：因果切片置位失败不得静默放行（验收用例⑫）。

事实来源：施工方案 v2.2 §5.4.1 子项④（L737）+ P0-08 卡子项④/验收用例⑫（L737-739）；
被测代码：engine.py on_market_event 置位段（原 L489-494 裸 ``except Exception: pass``）、
mixed_engine.py ``_execute_signals``（原 L802-806 裸 ``except Exception: pass``）/
``_execute_target``（#335 已显式降级，本卡统一补哨兵计数 + 写 context_sliced=false）。

判据（用例⑫）：构造 bar.timestamp 异常（越界 float 1e20——``datetime.fromtimestamp``
与 ``pd.Timestamp(..., unit='s')`` 均抛 OverflowError，且 ``_bar_date_str`` 哨兵
M1A-04 可吞不提前抛）→ 引擎必须显式降级而非静默放行：
  1. ``_slicing_enabled`` 保持 False（get_data 若返回全量将含未来——不可静默全量）
  2. ``_slicing_failures`` 哨兵计数 +1（可观测，不再无声）
  3. manifest ``causality.context_sliced`` 立即写 false——子项⑤ checks/causality 判
     critical 的输入条件（context_sliced=false 且 full_sample_declared=false → critical），
     本测试断言该条件成立；⑤落地后由 test_checks_causality 断言实际 critical 违规。
"""
from __future__ import annotations

import time
from typing import List

from myquant.backtest.engine import BacktestEngine
from myquant.backtest.mixed_engine import MixedBacktestEngine
from myquant.models.market_data import Bar
from myquant.realism_gate.manifest import RealismManifest
from myquant.strategies.base import StrategyContext

# 越界秒级时间戳：datetime.fromtimestamp 与 pd.Timestamp(unit='s') 均抛 OverflowError，
# 且 _bar_date_str 哨兵（M1A-04）可吞（不提前抛），确保走到切片置位段
BAD_TIMESTAMP: float = 1e20
# 正常秒级时间戳（time.time() 口径，与生产 Bar.timestamp 一致）
GOOD_TIMESTAMP: float = time.time()


class _ProbeStrategy:
    """最小探针策略：事件信号返回空、目标持仓模式返回 None（不触发后续撮合）。"""

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[object]:
        return []

    def generate_mixed_loop(
        self, bar: Bar, context: StrategyContext, position: object
    ) -> None:
        return None


def _make_manifest() -> RealismManifest:
    """构造最小 causality 组 manifest（模拟 create_engine 装配期注入）。"""
    manifest = RealismManifest()
    manifest.update_group(
        "causality",
        {
            "context_sliced": True,
            "current_bar_ts_enforced": False,
            "full_sample_declared": False,
            "guard_injected": False,
        },
    )
    return manifest


def _assert_fail_open_guard(
    engine: object, context: StrategyContext, manifest: RealismManifest
) -> None:
    """用例⑫断言：显式降级三要素 + 子项⑤ critical 判据输入条件成立。"""
    # 1. 不得静默放行（保持未封门，_slicing_enabled 仍 False）
    assert context._slicing_enabled is False
    # 2. 哨兵计数 +1（可观测，不再无声）
    assert engine._slicing_failures == 1  # type: ignore[attr-defined]
    # 3. 立即写 context_sliced=false
    assert manifest.get_field("causality.context_sliced") is False
    # 子项⑤ critical 判据输入：context_sliced=false 且未声明全量逃生口
    assert manifest.get_field("causality.full_sample_declared") is False


def test_event_engine_slicing_fail_is_not_silent() -> None:
    """event on_market_event：bar.timestamp 非法 → 显式降级（用例⑫）。"""
    engine = BacktestEngine(config=None)
    engine.context = StrategyContext()
    engine._realism_manifest = _make_manifest()
    engine.on_market_event(Bar(timestamp=BAD_TIMESTAMP))
    _assert_fail_open_guard(engine, engine.context, engine._realism_manifest)


def test_mixed_execute_signals_slicing_fail_is_not_silent() -> None:
    """mixed 事件信号路径：bar.timestamp 非法 → 显式降级（用例⑫）。"""
    engine = MixedBacktestEngine(config=None, strategy=_ProbeStrategy())
    engine.context = StrategyContext()
    engine._realism_manifest = _make_manifest()
    engine._execute_signals(Bar(timestamp=BAD_TIMESTAMP))
    _assert_fail_open_guard(engine, engine.context, engine._realism_manifest)


def test_mixed_execute_target_slicing_fail_is_not_silent() -> None:
    """mixed TARGET 路径（#335 显式降级升级）：补哨兵计数 + 写 context_sliced=false（用例⑫）。"""
    engine = MixedBacktestEngine(config=None, strategy=_ProbeStrategy())
    engine.context = StrategyContext()
    engine._realism_manifest = _make_manifest()
    engine._execute_target(Bar(timestamp=BAD_TIMESTAMP))
    _assert_fail_open_guard(engine, engine.context, engine._realism_manifest)


def test_normal_timestamp_succeeds() -> None:
    """正常 timestamp：置位成功、哨兵计数不增、manifest 不误写 false（不误伤）。"""
    engine = BacktestEngine(config=None)
    engine.context = StrategyContext()
    engine._realism_manifest = _make_manifest()
    engine.on_market_event(Bar(timestamp=GOOD_TIMESTAMP))
    assert engine.context._slicing_enabled is True
    assert engine._slicing_failures == 0
    assert engine._realism_manifest.get_field("causality.context_sliced") is True
