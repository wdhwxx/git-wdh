# 文件名：tests/unit/test_data_fingerprint.py
"""
test_data_fingerprint.py - P3-2 数据指纹回归单元测试
====================================================
覆盖 myquant/utils/data_fingerprint.py：
  - DataFingerprint   指纹值对象（字段 / to_dict / from_dict / quick_match / ==）
  - compute_fingerprint / compute_fingerprint_from_file  指纹计算
  - save_fingerprint / load_fingerprint  指纹持久化（原子写）
  - FingerprintStore  多标的指纹仓库（变更检测 / sync / 持久化）

测试维度:
  1. 指纹计算：确定性、内容敏感、日期/列结构、防御校验
  2. 指纹比较：quick_match 结构过滤、== 完整比较、fingerprints_equal
  3. 文件指纹：CSV / Parquet / 非法格式 / 文件缺失
  4. 持久化：save/load 往返、原子写
  5. 仓库：增删查、变更检测（新增/变更/未变）、sync 持久化

运行方式:
  pytest tests/unit/test_data_fingerprint.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from myquant.utils.data_fingerprint import (
    DEFAULT_HASH_ALGORITHM,
    DataFingerprint,
    FingerprintStore,
    compute_fingerprint,
    compute_fingerprint_from_file,
    fingerprints_equal,
    load_fingerprint,
    save_fingerprint,
)

# ─────────────────────────────────────────────
#  辅助：构造标准行情 DataFrame
# ─────────────────────────────────────────────

def _make_df(n_rows: int = 60, start: str = "2023-01-02") -> pd.DataFrame:
    """构造带日期索引的标准 OHLCV 行情。"""
    dates = pd.date_range(start, periods=n_rows, freq="B")
    return pd.DataFrame(
        {
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0,
            "volume": 10000.0,
        },
        index=dates,
    )


# ═════════════════════════════════════════════
#  1. 指纹计算 compute_fingerprint
# ═════════════════════════════════════════════

class TestComputeFingerprint:
    """指纹计算基础能力测试。"""

    def test_basic_fields(self) -> None:
        """基本字段：symbol / row_count / 列排序 / hash 非空 / 日期范围。"""
        df = _make_df(60)
        fp = compute_fingerprint(df, symbol="600519.SH")
        assert fp.symbol == "600519.SH"
        assert fp.row_count == 60
        assert fp.columns == tuple(sorted(df.columns))
        assert len(fp.hash_value) == 64  # sha256 hex
        assert fp.start_date == "2023-01-02"
        assert fp.end_date == "2023-03-24"  # 60 个工作日
        assert fp.algorithm == DEFAULT_HASH_ALGORITHM
        assert fp.frequency == "daily"

    def test_deterministic_same_data(self) -> None:
        """相同数据应产生相同指纹（内容哈希一致）。"""
        fp1 = compute_fingerprint(_make_df(60), "600519.SH")
        fp2 = compute_fingerprint(_make_df(60), "600519.SH")
        assert fp1.hash_value == fp2.hash_value
        assert fp1 == fp2

    def test_content_change_changes_hash(self) -> None:
        """内容变化应改变 hash_value。"""
        df1 = _make_df(60)
        df2 = _make_df(60)
        df2.loc[df2.index[10], "close"] = 11.0
        fp1 = compute_fingerprint(df1, "600519.SH")
        fp2 = compute_fingerprint(df2, "600519.SH")
        assert fp1.hash_value != fp2.hash_value

    def test_row_count_change_detected(self) -> None:
        """行数变化应改变指纹（row_count 与 hash 均不同）。"""
        fp1 = compute_fingerprint(_make_df(60), "600519.SH")
        fp2 = compute_fingerprint(_make_df(61), "600519.SH")
        assert fp1.row_count != fp2.row_count
        assert fp1 != fp2

    def test_no_date_column_has_empty_dates(self) -> None:
        """无日期列（RangeIndex）应得到空日期范围。"""
        df = pd.DataFrame({"close": [10.0, 10.1, 10.2]})
        fp = compute_fingerprint(df, "600519.SH")
        assert fp.start_date == ""
        assert fp.end_date == ""
        assert fp.row_count == 3

    def test_metadata_and_source(self) -> None:
        """metadata / data_source / frequency 应被记录。"""
        fp = compute_fingerprint(
            _make_df(10),
            "600519.SH",
            frequency="1d",
            data_source="akshare",
            metadata={"adj": "qfq", "version": "1.0"},
        )
        assert fp.frequency == "1d"
        assert fp.data_source == "akshare"
        assert fp.metadata == {"adj": "qfq", "version": "1.0"}

    def test_empty_symbol_raises(self) -> None:
        """空 symbol 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_fingerprint(_make_df(10), symbol="  ")

    def test_non_dataframe_raises(self) -> None:
        """非 DataFrame 应抛出 TypeError。"""
        with pytest.raises(TypeError):
            compute_fingerprint([1, 2, 3], symbol="600519.SH")  # type: ignore[arg-type]


# ═════════════════════════════════════════════
#  2. 指纹比较与序列化
# ═════════════════════════════════════════════

class TestFingerprintCompare:
    """指纹比较 / 序列化测试。"""

    def test_quick_match_structural(self) -> None:
        """quick_match 只比较结构（symbol/rows/date/columns），不比较 hash。"""
        fp1 = compute_fingerprint(_make_df(60), "600519.SH")
        fp2 = compute_fingerprint(_make_df(60), "600519.SH")
        assert fp1.quick_match(fp2) is True
        # 不同 symbol → 结构不匹配
        fp3 = compute_fingerprint(_make_df(60), "000001.SZ")
        assert fp1.quick_match(fp3) is False

    def test_equality_requires_hash(self) -> None:
        """== 应比较内容哈希（内容变化即不等）。"""
        df1 = _make_df(60)
        df2 = _make_df(60)
        df2.loc[df2.index[0], "volume"] = 99999.0
        fp1 = compute_fingerprint(df1, "600519.SH")
        fp2 = compute_fingerprint(df2, "600519.SH")
        assert fp1 != fp2
        assert fingerprints_equal(fp1, fp2) is False

    def test_to_dict_from_dict_roundtrip(self) -> None:
        """to_dict / from_dict 应往返无损。"""
        fp = compute_fingerprint(
            _make_df(30), "600519.SH", metadata={"adj": "hfq"}
        )
        d = fp.to_dict()
        assert isinstance(d["columns"], list)
        restored = DataFingerprint.from_dict(d)
        assert restored == fp
        assert restored.columns == fp.columns

    def test_from_dict_missing_field_raises(self) -> None:
        """from_dict 缺少必要字段应抛出 KeyError。"""
        with pytest.raises(KeyError):
            DataFingerprint.from_dict({"symbol": "600519.SH"})

    def test_hash_and_repr(self) -> None:
        """hash() 可用且 repr 含关键信息。"""
        fp = compute_fingerprint(_make_df(10), "600519.SH")
        assert isinstance(hash(fp), int)
        assert "600519.SH" in repr(fp)
        assert "rows=10" in repr(fp)


# ═════════════════════════════════════════════
#  3. 文件指纹 compute_fingerprint_from_file
# ═════════════════════════════════════════════

class TestFingerprintFromFile:
    """文件级指纹计算测试。"""

    def test_csv_roundtrip(self, tmp_path: Path) -> None:
        """CSV 文件指纹应与直接读取该文件后的 DataFrame 指纹一致。"""
        df = _make_df(30)
        path = tmp_path / "data.csv"
        df.to_csv(path)
        fp_file = compute_fingerprint_from_file(path, "600519.SH")
        read_df = pd.read_csv(path)
        fp_read = compute_fingerprint(read_df, "600519.SH")
        assert fp_file.hash_value == fp_read.hash_value
        assert fp_file.row_count == 30

    def test_parquet_roundtrip(self, tmp_path: Path) -> None:
        """Parquet 文件指纹应等于同内容 DataFrame 指纹。"""
        df = _make_df(30)
        path = tmp_path / "data.parquet"
        df.to_parquet(path)
        fp_file = compute_fingerprint_from_file(path, "600519.SH")
        fp_df = compute_fingerprint(df, "600519.SH")
        assert fp_file.hash_value == fp_df.hash_value

    def test_unsupported_format_raises(self, tmp_path: Path) -> None:
        """不支持的文件格式应抛出 ValueError。"""
        path = tmp_path / "data.txt"
        path.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            compute_fingerprint_from_file(path, "600519.SH")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """文件不存在应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            compute_fingerprint_from_file(tmp_path / "nope.csv", "600519.SH")


# ═════════════════════════════════════════════
#  4. 指纹持久化 save / load
# ═════════════════════════════════════════════

class TestFingerprintPersistence:
    """指纹持久化测试。"""

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        """save / load 应往返无损。"""
        fp = compute_fingerprint(_make_df(30), "600519.SH", metadata={"adj": "qfq"})
        path = tmp_path / "fp.json"
        save_fingerprint(fp, path)
        loaded = load_fingerprint(path)
        assert loaded == fp
        assert loaded.metadata == fp.metadata

    def test_saved_file_is_valid_json(self, tmp_path: Path) -> None:
        """保存文件应为合法 JSON（带 format_version + fingerprint 封装）。"""
        fp = compute_fingerprint(_make_df(10), "600519.SH")
        path = tmp_path / "fp.json"
        save_fingerprint(fp, path)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        assert d["format_version"]  # 非空版本号
        assert d["fingerprint"]["symbol"] == "600519.SH"
        assert d["fingerprint"]["hash_value"] == fp.hash_value


# ═════════════════════════════════════════════
#  5. FingerprintStore 多标的仓库
# ═════════════════════════════════════════════

class TestFingerprintStore:
    """指纹仓库测试。"""

    def test_put_get_has_remove(self, tmp_path: Path) -> None:
        """基础增删查。"""
        store = FingerprintStore(tmp_path / "fp_store.json")
        fp = compute_fingerprint(_make_df(10), "600519.SH")
        assert store.has("600519.SH") is False
        store.put(fp)
        assert store.has("600519.SH") is True
        assert "600519.SH" in store
        assert store.get("600519.SH") == fp
        assert len(store) == 1
        assert store.all_symbols() == ["600519.SH"]
        assert store.remove("600519.SH") is True
        assert store.remove("600519.SH") is False  # 二次删除返回 False
        assert store.get("600519.SH") is None

    def test_get_changed_symbols(self, tmp_path: Path) -> None:
        """变更检测：新增 / 内容变更 / 未变更。"""
        store = FingerprintStore(tmp_path / "fp_store.json")
        fp_a1 = compute_fingerprint(_make_df(10), "000001.SZ")
        fp_b1 = compute_fingerprint(_make_df(20), "000002.SZ")
        store.put(fp_a1)
        store.put(fp_b1)

        # 内容变更的 A + 未变更的 B + 新增的 C
        df_a2 = _make_df(10)
        df_a2.loc[df_a2.index[0], "close"] = 99.0
        fp_a2 = compute_fingerprint(df_a2, "000001.SZ")
        fp_c = compute_fingerprint(_make_df(30), "000003.SZ")
        changed = store.get_changed_symbols(
            {"000001.SZ": fp_a2, "000002.SZ": fp_b1, "000003.SZ": fp_c}
        )
        assert changed == ["000001.SZ", "000003.SZ"]

    def test_has_changed(self, tmp_path: Path) -> None:
        """has_changed：无历史视为变更；内容一致视为未变更。"""
        store = FingerprintStore(tmp_path / "fp_store.json")
        fp = compute_fingerprint(_make_df(10), "000001.SZ")
        assert store.has_changed("000001.SZ", fp) is True  # 新增
        store.put(fp)
        assert store.has_changed("000001.SZ", fp) is False  # 未变

    def test_sync_returns_changed_and_persists(self, tmp_path: Path) -> None:
        """sync 应返回变更列表并原子持久化到磁盘。"""
        store_path = tmp_path / "fp_store.json"
        store = FingerprintStore(store_path)
        fp_a1 = compute_fingerprint(_make_df(10), "000001.SZ")
        store.put(fp_a1)

        fp_a2 = compute_fingerprint(_make_df(11), "000001.SZ")
        fp_b = compute_fingerprint(_make_df(20), "000002.SZ")
        changed = store.sync({"000001.SZ": fp_a2, "000002.SZ": fp_b})
        assert changed == ["000001.SZ", "000002.SZ"]

        # 重开仓库应从磁盘恢复
        reopened = FingerprintStore(store_path)
        assert reopened.get("000001.SZ") == fp_a2
        assert reopened.get("000002.SZ") == fp_b
        assert len(reopened) == 2

    def test_auto_initializes_empty(self, tmp_path: Path) -> None:
        """文件不存在时应自动初始化为空仓库。"""
        store = FingerprintStore(tmp_path / "missing_store.json")
        assert len(store) == 0
        assert store.all_symbols() == []

    def test_save_then_reopen(self, tmp_path: Path) -> None:
        """save() 显式持久化后可重开读取。"""
        store_path = tmp_path / "fp_store.json"
        store = FingerprintStore(store_path)
        fp = compute_fingerprint(_make_df(10), "600519.SH")
        store.put(fp)
        store.save()
        reopened = FingerprintStore(store_path)
        assert reopened.get("600519.SH") == fp
