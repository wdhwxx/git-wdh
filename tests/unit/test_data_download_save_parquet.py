# 文件名：tests/unit/test_data_download_save_parquet.py
"""数据下载 GUI _save_parquet 合并去重单测。

背景：增量更新日线（force_full=False）只拉取缺失区间，若 _save_parquet 直接覆盖写
会把 processed parquet 截断成只剩增量区间、丢弃存量全历史。本测试验证修复后的
合并去重写入：存量历史 + 本次区间按日期合并、去重（保留最新）、升序回写。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

from myquant.gui.data_download_dialog import _save_parquet


class _FakeResult:
    """模拟 MarketDataResult，仅提供 _save_parquet 用到的 symbol 与 to_dataframe。"""

    def __init__(self, symbol: str, df: pd.DataFrame) -> None:
        self.symbol = symbol
        self._df = df

    def to_dataframe(self) -> pd.DataFrame:
        """返回行情数据帧（与 MarketDataResult 接口一致）。"""
        return self._df


def _make_slice(dates: List[int]) -> pd.DataFrame:
    """构造增量区间数据帧（仅含日期与收盘价两列，足够覆盖合并逻辑）。"""
    return pd.DataFrame({"日期": dates, "收盘价": [float(i + 1) for i in range(len(dates))]})


def _write_existing(project_root: Path, symbol: str, dates: List[int]) -> Path:
    """在沙箱 processed 目录预置存量 parquet（全历史）。"""
    proc = project_root / "datasets" / "processed"
    proc.mkdir(parents=True, exist_ok=True)
    path = proc / f"{symbol}.parquet"
    pd.DataFrame({"日期": dates, "收盘价": [float(i + 1) for i in range(len(dates))]}).to_parquet(path)
    return path


def _read(project_root: Path, symbol: str) -> pd.DataFrame:
    return pd.read_parquet(project_root / "datasets" / "processed" / f"{symbol}.parquet")


def test_save_parquet_增量合并保留全历史并追加新日(tmp_path: Path) -> None:
    """存量全历史 + 增量新日 → 合并后最早历史仍在、新日追加、日期升序（不丢历史）。"""
    symbol = "TST.SZ"
    _write_existing(tmp_path, symbol, [19910403, 20000101, 20260918])
    out = _save_parquet(_FakeResult(symbol, _make_slice([20260921, 20260922, 20260923])), tmp_path)

    assert out.name == f"{symbol}.parquet"
    df = _read(tmp_path, symbol)
    assert len(df) == 6
    assert df["日期"].is_monotonic_increasing
    assert int(df["日期"].min()) == 19910403  # 历史最早日期保留
    assert int(df["日期"].max()) == 20260923  # 新日追加


def test_save_parquet_重叠日去重保留最新值(tmp_path: Path) -> None:
    """增量区间与存量重叠日 → 去重为 1，且保留最新（keep=last 取本次新值）。"""
    symbol = "TST.SZ"
    _write_existing(tmp_path, symbol, [19910403, 20260918])  # 存量 20260918 收盘价=2.0
    df_new = pd.DataFrame({"日期": [20260918, 20260921], "收盘价": [9.0, 9.1]})
    _save_parquet(_FakeResult(symbol, df_new), tmp_path)

    df = _read(tmp_path, symbol)
    assert len(df) == 3
    assert int((df["日期"] == 20260918).sum()) == 1  # 重叠日去重
    assert int(df.loc[df["日期"] == 20260918, "收盘价"].iloc[0]) == 9  # keep=last 取新值


def test_save_parquet_无存量新建(tmp_path: Path) -> None:
    """processed 不存在该标的 → 仅写入本次区间，正常返回路径。"""
    symbol = "NEW.SZ"
    out = _save_parquet(_FakeResult(symbol, _make_slice([20260921, 20260922])), tmp_path)
    df = _read(tmp_path, symbol)
    assert out.name == f"{symbol}.parquet"
    assert len(df) == 2
    assert int(df["日期"].min()) == 20260921


def test_save_parquet_空结果不改写存量(tmp_path: Path) -> None:
    """本次区间为空 → 不改写文件，存量 parquet 保持不变。"""
    symbol = "TST.SZ"
    existing = _write_existing(tmp_path, symbol, [19910403, 20260918])
    before = pd.read_parquet(existing)
    out = _save_parquet(_FakeResult(symbol, _make_slice([])), tmp_path)
    after = pd.read_parquet(out)
    assert len(after) == len(before) == 2
    assert int(after["日期"].min()) == 19910403


def test_save_parquet_存量损坏降级不清洗(tmp_path: Path) -> None:
    """存量 parquet 损坏 → 不崩溃，警告后仅写入本次区间。"""
    symbol = "TST.SZ"
    proc = tmp_path / "datasets" / "processed"
    proc.mkdir(parents=True, exist_ok=True)
    bad = proc / f"{symbol}.parquet"
    bad.write_bytes(b"\x00\x01not-a-valid-parquet")  # 损坏文件
    out = _save_parquet(_FakeResult(symbol, _make_slice([20260921])), tmp_path)
    df = _read(tmp_path, symbol)
    assert out.name == f"{symbol}.parquet"
    assert len(df) == 1
    assert int(df["日期"].iloc[0]) == 20260921


def test_save_parquet_缺日期列不抛异常(tmp_path: Path) -> None:
    """本次数据缺 日期 列 → 不做去重/排序，仅拼接写回，不抛异常。"""
    symbol = "TST.SZ"
    _write_existing(tmp_path, symbol, [19910403, 20260918])
    df_no_date = pd.DataFrame({"收盘价": [1.0, 2.0]})
    _save_parquet(_FakeResult(symbol, df_no_date), tmp_path)
    df = _read(tmp_path, symbol)
    assert len(df) >= 2  # 未崩溃，正常写出
