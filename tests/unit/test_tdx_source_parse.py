# 文件名：tests/unit/test_tdx_source_parse.py
"""
P1-⑤（#83）TDX .day 解析：amount float32→float64 语义锁定 + volume 股单位。
"""

from __future__ import annotations

import struct

import pytest

from myquant.data_access.sources.tdx_source import TdxDataSource


def _day_bytes(amount: float = 12345678.5, volume: int = 5000000, date_int: int = 20240102) -> bytes:
    """构造 32 字节 .day 记录（TDX_RECORD_SIZE=32）。"""
    return struct.pack("<iiiiifii", date_int, 1000, 1050, 990, 1020, amount, volume, 0)


class TestTdxParse:
    def test_amount_float64_and_volume_share(self, tmp_path):
        """amount 为 float64（float32 源精度不可逆，锁定为 unpack 值）；volume 为股。"""
        p = tmp_path / "sh600519.day"
        p.write_bytes(_day_bytes())
        src = TdxDataSource()
        recs = src._parse_day_file(p)
        assert len(recs) == 1
        r = recs[0]
        assert isinstance(r["amount"], float)
        f32_value = struct.unpack("<f", struct.pack("<f", 12345678.5))[0]
        assert r["amount"] == round(float(f32_value), 2)
        assert r["volume"] == 5000000.0  # 股（.day 源实为股，勿 ×100）

    def test_malformed_file_empty(self, tmp_path):
        """非 32 字节倍数的文件返回空（不崩溃）。"""
        p = tmp_path / "bad.day"
        p.write_bytes(b"\x00\x01")
        src = TdxDataSource()
        assert src._parse_day_file(p) == []
