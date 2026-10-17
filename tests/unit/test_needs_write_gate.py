# 文件名：tests/unit/test_needs_write_gate.py
"""
C-04 needs_write 恒真式修复单测（V-12 无变更跳过写回）
========================================================
覆盖（蓝图 #451 配套，直接驱动 _evaluate_needs_write 纯函数，零网络零 DB）：
  1. 恒真消除：无 bad / 无去重 / 行数同 / 派生列全同 → needs_write=False（真正无变更跳过写回）
  2. 派生量保护：派生列任一变化（pct_chg / turnover / float_share）→ needs_write=True（不回归派生量写回）
  3. 既有触发条件保持：bad 非空 / 去重>0 / 行数变化 → True
  4. worker_clean 接线：模块内引用 _evaluate_needs_write（防绕过）
  5. 派生列常量与 derived_metrics 产出列一致（防漏列）
"""
from __future__ import annotations

from typing import Any, Dict, List

from myquant.data_access.full_clean_service import (
    _DERIVED_WRITEBACK_COLS,
    _evaluate_needs_write,
)


def _row(date: str, close: float, turnover: float = 0.1,
         pct_chg: Any = 1.0, amplitude: Any = 2.0,
         volume_ratio: Any = 1.0, float_share: Any = 1e10,
         pe_dynamic: Any = 30.0) -> Dict[str, Any]:
    """构造一条清洗后行（含派生列）。"""
    return {
        "symbol": "000001.SZ", "trade_date": date, "period": "daily",
        "adjust": "raw", "open": close * 0.99, "high": close * 1.01,
        "low": close * 0.98, "close": close, "volume": 1e6,
        "amount": 1e7, "is_suspended": 0, "data_source": "tdx",
        "turnover": turnover, "pct_chg": pct_chg, "amplitude": amplitude,
        "volume_ratio": volume_ratio, "float_share": float_share,
        "pe_dynamic": pe_dynamic,
    }


def _rows(n: int) -> List[Dict[str, Any]]:
    return [_row(f"2024-01-{i:02d}", 10.0 + i) for i in range(1, n + 1)]


def _stats(removed: int = 0) -> Dict[str, Any]:
    return {"removed_duplicates": removed}


# ── 1. 恒真消除：完全无变更 → False ──────────────────────────
def test_no_change_skips_write() -> None:
    """无 bad / 无去重 / 行数同 / 派生列全同 → needs_write=False。"""
    rows = _rows(2)
    cleaned = [dict(r) for r in rows]  # 派生列与 DB 原值一致（无变化）
    assert _evaluate_needs_write(rows, cleaned, [], _stats()) is False


# ── 2. 派生量保护：派生列变化 → True ─────────────────────────
def test_derived_change_triggers_write() -> None:
    """pct_chg 变化（派生量更新）→ needs_write=True（不回归派生量写回）。"""
    rows = _rows(2)
    cleaned = [dict(r) for r in rows]
    cleaned[0]["pct_chg"] = 9.99
    assert _evaluate_needs_write(rows, cleaned, [], _stats()) is True


def test_turnover_change_triggers_write() -> None:
    """turnover（B 类派生）变化 → needs_write=True。"""
    rows = _rows(2)
    cleaned = [dict(r) for r in rows]
    cleaned[1]["turnover"] = 0.88
    assert _evaluate_needs_write(rows, cleaned, [], _stats()) is True


def test_float_share_change_triggers_write() -> None:
    """float_share（B 类派生）变化 → needs_write=True。"""
    rows = _rows(2)
    cleaned = [dict(r) for r in rows]
    cleaned[0]["float_share"] = 2e10
    assert _evaluate_needs_write(rows, cleaned, [], _stats()) is True


# ── 3. 既有触发条件保持 ──────────────────────────────────────
def test_bad_triggers_write() -> None:
    """bad 非空 → needs_write=True（历史语义保持）。"""
    rows = _rows(2)
    cleaned = [dict(r) for r in rows]
    bad = [{"date": "2024-01-01", "_errors": ["x"]}]
    assert _evaluate_needs_write(rows, cleaned, bad, _stats()) is True


def test_removed_duplicates_triggers_write() -> None:
    """去重 > 0 → needs_write=True（历史语义保持）。"""
    rows = _rows(2)
    cleaned = [dict(r) for r in rows]
    assert _evaluate_needs_write(rows, cleaned, [], _stats(removed=1)) is True


def test_row_count_change_triggers_write() -> None:
    """行数变化 → needs_write=True（历史语义保持）。"""
    rows = _rows(3)
    cleaned = _rows(2)  # 少一行（脏行剔除）
    assert _evaluate_needs_write(rows, cleaned, [], _stats()) is True


# ── 4. worker_clean 接线引用（防绕过）────────────────────────
def test_worker_clean_uses_evaluator() -> None:
    """worker_clean 必须引用 _evaluate_needs_write（接线防回归）。"""
    import inspect

    import myquant.data_access.full_clean_service as fcs
    src = inspect.getsource(fcs.worker_clean)
    assert "_evaluate_needs_write(" in src
    assert "True or" not in src, "恒真式不得回归"


# ── 5. 派生列常量完整性 ──────────────────────────────────────
def test_derived_writeback_cols_constant() -> None:
    """派生保护列常量覆盖 A/B 类派生列。"""
    from myquant.data_access.cleaner.derived_metrics import compute_derived_metrics
    sample = [{"trade_date": "2024-01-02", "open": 10.0, "high": 10.5,
               "low": 9.8, "close": 10.2, "volume": 1e6},
              {"trade_date": "2024-01-03", "open": 10.2, "high": 10.8,
               "low": 10.0, "close": 10.7, "volume": 1.2e6}]
    out = compute_derived_metrics(sample)
    assert out[1]["pct_chg"] is not None
    for col in ("pct_chg", "amplitude", "volume_ratio", "turnover",
                "float_share", "pe_dynamic"):
        assert col in _DERIVED_WRITEBACK_COLS
