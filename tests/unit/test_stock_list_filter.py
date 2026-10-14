# 文件名：tests/unit/test_stock_list_filter.py
"""
B-4（P2-④ #83）标的池过滤单测：
  get_stock_list 剔除 B 股（sh 90 / sz 20）与指数/可转债/基金
  补北交所 bj 目录（8/92 开头保留，非北交所剔除）
"""

from __future__ import annotations

from pathlib import Path

from myquant.data_access.sources.tdx_source import TdxDataSource


def _mk_tdx(tmp_path: Path):
    """构造含 sh/sz/bj 三市场 .day 的临时 TDX 根。"""
    files = {
        "sh/lday/sh600519.day": b"x",   # 沪主板 A 保留
        "sh/lday/sh688001.day": b"x",   # 科创板 A 保留
        "sh/lday/sh900901.day": b"x",   # 沪 B 股（900）剔除
        "sh/lday/sh000001.day": b"x",   # 沪指数 剔除
        "sh/lday/sh113001.day": b"x",   # 可转债 剔除
        "sz/lday/sz000001.day": b"x",   # 深主板 A 保留
        "sz/lday/sz300001.day": b"x",   # 创业板 A 保留
        "sz/lday/sz200002.day": b"x",   # 深 B 股（200）剔除
        "sz/lday/sz399001.day": b"x",   # 深指数 剔除
        "sz/lday/sz150001.day": b"x",   # 基金 剔除
        "bj/lday/bj832001.day": b"x",   # 北交所 8 开头 保留
        "bj/lday/bj920002.day": b"x",   # 北交所 920 开头 保留
        "bj/lday/bj430001.day": b"x",   # 老三板 4 开头 剔除
        "bj/lday/bj899050.day": b"x",   # 北证50 指数（899）剔除
        "bj/lday/bj810011.day": b"x",   # 81 段非股票 剔除
    }
    for rel, content in files.items():
        p = tmp_path / "vipdoc" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return TdxDataSource(config={"tdx_root": str(tmp_path), "auto_detect": False})


class TestGetStockListFilter:
    def test_filter_b_share_and_index(self, tmp_path: Path):
        """B 股/指数/可转债/基金剔除，沪深 A + 北交所保留。"""
        src = _mk_tdx(tmp_path)
        stocks = src.get_stock_list()
        symbols = sorted(s["symbol"] for s in stocks)
        assert symbols == [
            "000001.SZ", "300001.SZ",
            "600519.SH", "688001.SH",
            "832001.BJ", "920002.BJ",
        ]
        # 无 B 股
        assert all(not s.startswith(("900", "200")) for s in symbols)
        # 北交所已纳入
        assert any(s.endswith(".BJ") for s in symbols)

    def test_no_bj_dir_ok(self, tmp_path: Path):
        """无 bj 目录不崩溃（旧环境兼容）。"""
        p = tmp_path / "vipdoc" / "sh" / "lday"
        p.mkdir(parents=True)
        (p / "sh600519.day").write_bytes(b"x")
        src = TdxDataSource(config={"tdx_root": str(tmp_path), "auto_detect": False})
        stocks = src.get_stock_list()
        assert [s["symbol"] for s in stocks] == ["600519.SH"]

    def test_no_tdx_root_empty(self):
        """无 tdx 根返回空。"""
        src = TdxDataSource(config={"tdx_root": None, "auto_detect": False})
        assert src.get_stock_list() == []
