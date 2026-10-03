# 文件名：myquant/utils/data_fingerprint.py
"""
data_fingerprint.py - 数据指纹模块
==================================
作为缓存失效、增量回测的底层基础。

功能清单:
  1. DataFingerprint 数据指纹值对象 (symbol / hash / row_count / date_range / columns)
  2. 从 pandas DataFrame 计算确定性指纹 (SHA-256, 行/列顺序无关)
  3. 从 CSV / Parquet 文件计算指纹
  4. 单个指纹持久化 (JSON 原子写入 / 加载)
  5. FingerprintStore 多标的指纹仓库 (线程安全、原子写入、变更检测)
  6. 增量回测辅助: get_changed_symbols / sync 识别变更标的

设计原则:
  - 同一份数据 (相同内容+结构) 必须产生相同指纹;
  - 任意数据内容或结构变更必须导致指纹变化;
  - 行顺序、列顺序不影响指纹 (内部排序后哈希);
  - 本模块不做任何时间序列计算, 无未来函数风险。

依赖: hashlib, json, os, pathlib, logging, dataclasses, threading, tempfile, typing
第三方: pandas (DataFrame 指纹计算); pyarrow/fastparquet (Parquet 读取, 可选)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

DEFAULT_HASH_ALGORITHM: str = "sha256"
DEFAULT_FREQUENCY: str = "daily"
DEFAULT_DATA_SOURCE: str = "unknown"
FINGERPRINT_FORMAT_VERSION: str = "1.0"

# 日期列候选名 (按优先级排序, 全小写匹配)
DATE_COLUMN_CANDIDATES: Tuple[str, ...] = (
    "date", "datetime", "trade_date", "time", "timestamp", "trade_time",
)

# 支持的文件格式
SUPPORTED_FILE_FORMATS: Tuple[str, ...] = (".csv", ".parquet", ".pq")

# 哈希列数据时的列间分隔符 (不可出现在正常数据中)
_COL_SEPARATOR: bytes = b"\x00"
_STR_VALUE_SEPARATOR: str = "\x01"


# ─────────────────────────────────────────────
# DataFingerprint 值对象
# ─────────────────────────────────────────────

@dataclass
class DataFingerprint:
    """数据指纹值对象，唯一标识一份行情数据的内容与结构。

    用于缓存失效判断、增量回测变更检测。
    同一份数据 (相同内容、相同结构) 必须产生相同的指纹。

    Attributes:
        symbol: 标的代码 (如 "600519.SH")。
        hash_value: 内容哈希 (十六进制字符串)。
        row_count: 数据行数。
        start_date: 起始日期 (ISO 8601 字符串, 无日期列时为空串)。
        end_date: 结束日期 (ISO 8601 字符串)。
        columns: 列名元组 (已排序)。
        algorithm: 哈希算法名称。
        frequency: 数据频率 ("daily", "minute", "tick" 等)。
        data_source: 数据来源标识 (如 "akshare", "tushare", "local")。
        metadata: 额外元数据 (如复权类型、数据版本)。
        created_at: 指纹生成时间 (ISO 8601, UTC)。
    """

    symbol: str
    hash_value: str
    row_count: int
    start_date: str
    end_date: str
    columns: Tuple[str, ...]
    algorithm: str = DEFAULT_HASH_ALGORITHM
    frequency: str = DEFAULT_FREQUENCY
    data_source: str = DEFAULT_DATA_SOURCE
    metadata: Dict[str, str] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典 (columns 从 tuple 转 list 以便 JSON 序列化)。

        Returns:
            可直接 json.dumps 的字典。
        """
        d = asdict(self)
        d["columns"] = list(self.columns)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DataFingerprint:
        """从字典反序列化为 DataFingerprint。

        Args:
            data: 包含指纹字段的字典。

        Returns:
            DataFingerprint 实例。

        Raises:
            KeyError: 缺少必要字段 (symbol / hash_value / row_count)。
        """
        return cls(
            symbol=data["symbol"],
            hash_value=data["hash_value"],
            row_count=data["row_count"],
            start_date=data.get("start_date", ""),
            end_date=data.get("end_date", ""),
            columns=tuple(data.get("columns", [])),
            algorithm=data.get("algorithm", DEFAULT_HASH_ALGORITHM),
            frequency=data.get("frequency", DEFAULT_FREQUENCY),
            data_source=data.get("data_source", DEFAULT_DATA_SOURCE),
            metadata=data.get("metadata", {}),
            created_at=data.get(
                "created_at", datetime.now(timezone.utc).isoformat()
            ),
        )

    def quick_match(self, other: DataFingerprint) -> bool:
        """快速结构匹配 (不比较完整 hash, 用于初步过滤)。

        注意: 返回 True 不代表数据完全相同, 需用 == 运算符确认。
        返回 False 则一定不同 (结构层面已可判定)。

        Args:
            other: 另一个指纹。

        Returns:
            结构是否一致 (symbol / row_count / date_range / columns)。
        """
        return (
            self.symbol == other.symbol
            and self.row_count == other.row_count
            and self.start_date == other.start_date
            and self.end_date == other.end_date
            and self.columns == other.columns
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DataFingerprint):
            return NotImplemented
        return (
            self.symbol == other.symbol
            and self.hash_value == other.hash_value
            and self.row_count == other.row_count
            and self.start_date == other.start_date
            and self.end_date == other.end_date
            and self.columns == other.columns
            and self.algorithm == other.algorithm
        )

    def __hash__(self) -> int:
        return hash((self.symbol, self.hash_value))

    def __repr__(self) -> str:
        return (
            f"DataFingerprint(symbol='{self.symbol}', rows={self.row_count}, "
            f"range={self.start_date}~{self.end_date}, "
            f"hash={self.hash_value[:12]}...)"
        )


# ─────────────────────────────────────────────
# 私有辅助函数
# ─────────────────────────────────────────────

def _detect_date_column(df: pd.DataFrame) -> Optional[str]:
    """检测 DataFrame 中的日期列。

    优先级:
      1. DatetimeIndex (返回 "__index__")
      2. 候选名列匹配 (date / datetime / trade_date / ...)
      3. 任意 datetime64 dtype 列

    Args:
        df: 待检测的 DataFrame。

    Returns:
        日期列名; 若 index 是 DatetimeIndex 返回 "__index__"; 未找到返回 None。
    """
    # 1. DatetimeIndex
    if isinstance(df.index, pd.DatetimeIndex):
        return "__index__"

    # 2. 候选名匹配 (大小写不敏感)
    col_lower_map = {col.lower(): col for col in df.columns}
    for candidate in DATE_COLUMN_CANDIDATES:
        if candidate in col_lower_map:
            return col_lower_map[candidate]

    # 3. 任意 datetime dtype 列
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            return col

    return None


def _format_timestamp(ts: Any) -> str:
    """将时间戳格式化为 ISO 8601 字符串。

    纯日期 (时分秒均为 0) 格式化为 "YYYY-MM-DD";
    含时间的格式化为 "YYYY-MM-DD HH:MM:SS"。

    Args:
        ts: pandas.Timestamp 或可转为字符串的时间值。

    Returns:
        格式化后的日期时间字符串。
    """
    if isinstance(ts, pd.Timestamp):
        if ts.hour == 0 and ts.minute == 0 and ts.second == 0:
            return ts.strftime("%Y-%m-%d")
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    return str(ts)


def _get_date_range(df: pd.DataFrame, date_col: Optional[str]) -> Tuple[str, str]:
    """提取数据的日期范围 (ISO 8601 字符串)。

    Args:
        df: 数据 DataFrame。
        date_col: 日期列名, "__index__" 表示使用索引, None 表示无日期列。

    Returns:
        (start_date, end_date) 元组; 无日期列或空数据时返回 ("", "")。
    """
    if date_col is None or df.empty:
        return ("", "")

    if date_col == "__index__":
        idx = df.index.sort_values()
        start, end = idx[0], idx[-1]
    else:
        series = df[date_col]
        # 字符串日期列尝试转为 datetime
        if not pd.api.types.is_datetime64_any_dtype(series):
            try:
                series = pd.to_datetime(series, errors="raise")
            except (ValueError, TypeError):
                # 无法解析, 退化为字符串 min/max (适用于 ISO 格式日期)
                sorted_vals = series.astype(str).sort_values()
                return (str(sorted_vals.iloc[0]), str(sorted_vals.iloc[-1]))
        sorted_vals = series.sort_values()
        start, end = sorted_vals.iloc[0], sorted_vals.iloc[-1]

    return (_format_timestamp(start), _format_timestamp(end))


def _compute_content_hash(
    df: pd.DataFrame, algorithm: str = DEFAULT_HASH_ALGORITHM
) -> str:
    """计算 DataFrame 内容的确定性哈希。

    哈希过程 (确保行/列顺序无关):
      1. 按索引排序 (消除行顺序影响)
      2. 哈希 schema (列名 + dtype, 按列名排序)
      3. 哈希行数
      4. 逐列哈希数据内容 (按列名排序, 消除列顺序影响)
         - 数值列: numpy 数组原始字节 (含 NaN 位模式)
         - 日期时间列: int64 纳秒原始字节
         - 其他列: 字符串拼接

    Args:
        df: 待哈希的 DataFrame。
        algorithm: 哈希算法名称 (如 "sha256", "md5")。

    Returns:
        十六进制哈希字符串。

    Raises:
        ValueError: 不支持的哈希算法。
    """
    try:
        h = hashlib.new(algorithm)
    except ValueError:
        raise ValueError(f"不支持的哈希算法: {algorithm!r}")

    # 1. 按索引排序 (空 DataFrame 也能处理)
    df_sorted = df.sort_index() if not df.empty else df

    # 2. 哈希 schema (列名 + dtype, 排序后)
    schema_parts = [
        f"{col}:{str(df_sorted[col].dtype)}"
        for col in sorted(df_sorted.columns)
    ]
    h.update("|".join(schema_parts).encode("utf-8"))
    h.update(_COL_SEPARATOR)

    # 3. 哈希行数
    h.update(str(len(df_sorted)).encode("utf-8"))
    h.update(_COL_SEPARATOR)

    # 3b. P1-19修复：哈希 index 值（此前仅 sort_index 不哈希索引，
    #     数值相同但日期区间不同的数据得同指纹→因子与行情错位）。
    if not df_sorted.empty:
        idx = df_sorted.index
        if pd.api.types.is_datetime64_any_dtype(idx):
            h.update(idx.astype("int64").to_numpy().tobytes())
        elif pd.api.types.is_numeric_dtype(idx):
            h.update(idx.to_numpy().tobytes())
        else:
            h.update(idx.astype(str).str.cat(sep=_STR_VALUE_SEPARATOR).encode("utf-8"))
    h.update(_COL_SEPARATOR)

    # 4. 逐列哈希数据内容
    for col in sorted(df_sorted.columns):
        series = df_sorted[col]
        h.update(col.encode("utf-8"))
        h.update(_COL_SEPARATOR)

        if pd.api.types.is_numeric_dtype(series):
            # 数值列: 直接使用 numpy 数组原始字节 (含 NaN 位模式)
            h.update(series.to_numpy().tobytes())
        elif pd.api.types.is_datetime64_any_dtype(series):
            # 日期时间列: 转为 int64 纳秒后取原始字节
            h.update(series.astype("int64").to_numpy().tobytes())
        else:
            # 对象/字符串/布尔等列: 转为字符串后拼接
            h.update(
                series.astype(str).str.cat(sep=_STR_VALUE_SEPARATOR).encode("utf-8")
            )
        h.update(_COL_SEPARATOR)

    return h.hexdigest()


# ─────────────────────────────────────────────
# 公开函数: 指纹计算
# ─────────────────────────────────────────────

def compute_fingerprint(
    df: pd.DataFrame,
    symbol: str,
    frequency: str = DEFAULT_FREQUENCY,
    data_source: str = DEFAULT_DATA_SOURCE,
    algorithm: str = DEFAULT_HASH_ALGORITHM,
    metadata: Optional[Dict[str, str]] = None,
) -> DataFingerprint:
    """从 pandas DataFrame 计算数据指纹。

    Args:
        df: 行情数据 DataFrame。
        symbol: 标的代码 (如 "600519.SH"), 必须为非空字符串。
        frequency: 数据频率 ("daily", "minute", "tick" 等)。
        data_source: 数据来源标识 (如 "akshare", "tushare", "local")。
        algorithm: 哈希算法 (默认 "sha256")。
        metadata: 额外元数据 (如复权类型、数据版本)。

    Returns:
        DataFingerprint 对象。

    Raises:
        ValueError: symbol 为空字符串。
        TypeError: df 不是 pandas DataFrame。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"symbol 必须是非空字符串, got: {symbol!r}")
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"df 必须是 pandas DataFrame, got: {type(df).__name__}")

    date_col = _detect_date_column(df)
    start_date, end_date = _get_date_range(df, date_col)
    hash_value = _compute_content_hash(df, algorithm)
    columns = tuple(sorted(df.columns))

    fp = DataFingerprint(
        symbol=symbol.strip(),
        hash_value=hash_value,
        row_count=len(df),
        start_date=start_date,
        end_date=end_date,
        columns=columns,
        algorithm=algorithm,
        frequency=frequency,
        data_source=data_source,
        metadata=metadata or {},
    )
    logger.debug(
        "计算指纹: symbol=%s, rows=%d, range=%s~%s, hash=%s",
        fp.symbol, fp.row_count, fp.start_date, fp.end_date, fp.hash_value[:16],
    )
    return fp


def compute_fingerprint_from_file(
    filepath: Union[str, Path],
    symbol: str,
    frequency: str = DEFAULT_FREQUENCY,
    data_source: str = DEFAULT_DATA_SOURCE,
    algorithm: str = DEFAULT_HASH_ALGORITHM,
    metadata: Optional[Dict[str, str]] = None,
    **read_kwargs: Any,
) -> DataFingerprint:
    """从数据文件 (CSV / Parquet) 计算指纹。

    Args:
        filepath: 文件路径 (.csv / .parquet / .pq)。
        symbol: 标的代码。
        frequency: 数据频率。
        data_source: 数据来源标识。
        algorithm: 哈希算法。
        metadata: 额外元数据。
        **read_kwargs: 透传给 pd.read_csv / pd.read_parquet 的参数。

    Returns:
        DataFingerprint 对象。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 不支持的文件格式。
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在: {path}")

    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path, **read_kwargs)
    elif suffix in (".parquet", ".pq"):
        df = pd.read_parquet(path, **read_kwargs)
    else:
        raise ValueError(
            f"不支持的文件格式: {suffix!r}, 支持: {SUPPORTED_FILE_FORMATS}"
        )

    return compute_fingerprint(
        df=df,
        symbol=symbol,
        frequency=frequency,
        data_source=data_source,
        algorithm=algorithm,
        metadata=metadata,
    )


# ─────────────────────────────────────────────
# 公开函数: 指纹比较与持久化
# ─────────────────────────────────────────────

def fingerprints_equal(fp1: DataFingerprint, fp2: DataFingerprint) -> bool:
    """判断两个指纹是否完全相同 (内容 + 结构)。

    Args:
        fp1: 第一个指纹。
        fp2: 第二个指纹。

    Returns:
        True 表示完全相同。
    """
    return fp1 == fp2


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """原子写入 JSON 文件 (先写临时文件再 rename, 防止写一半崩溃)。

    Args:
        path: 目标文件路径。
        data: 可序列化的字典数据。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, ensure_ascii=False)

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def save_fingerprint(fp: DataFingerprint, filepath: Union[str, Path]) -> None:
    """将单个指纹保存为 JSON 文件 (原子写入)。

    Args:
        fp: 数据指纹。
        filepath: 输出文件路径 (建议 .json)。
    """
    path = Path(filepath)
    data = {
        "format_version": FINGERPRINT_FORMAT_VERSION,
        "fingerprint": fp.to_dict(),
    }
    _atomic_write_json(path, data)
    logger.debug("指纹已保存: %s (symbol=%s)", path, fp.symbol)


def load_fingerprint(filepath: Union[str, Path]) -> DataFingerprint:
    """从 JSON 文件加载单个指纹。

    Args:
        filepath: 指纹文件路径。

    Returns:
        DataFingerprint 对象。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件格式不正确 (JSON 解析失败 / 缺少必要字段)。
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"指纹文件不存在: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"指纹文件 JSON 解析失败: {path}, 错误: {e}")

    fp_data = data.get("fingerprint")
    if fp_data is None:
        raise ValueError(f"指纹文件缺少 'fingerprint' 字段: {path}")

    try:
        return DataFingerprint.from_dict(fp_data)
    except KeyError as e:
        raise ValueError(f"指纹数据缺少必要字段: {e}, 文件: {path}")


# ─────────────────────────────────────────────
# FingerprintStore 多标的指纹仓库
# ─────────────────────────────────────────────

class FingerprintStore:
    """多标的数据指纹仓库，用于批量管理指纹与变更检测。

    特性:
      - 线程安全 (threading.Lock 保护读写)
      - 原子写入 (临时文件 + os.replace)
      - 增量回测支持: get_changed_symbols / sync 识别变更标的
      - 文件不存在时自动初始化为空仓库

    Usage:
        store = FingerprintStore("./cache/fingerprints.json")
        store.put(fp_600519)
        store.put(fp_000001)
        store.save()
        changed = store.get_changed_symbols(new_fps_dict)
    """

    def __init__(self, store_path: Union[str, Path]):
        """
        Args:
            store_path: 指纹仓库 JSON 文件路径 (不存在则自动创建)。
        """
        self._path = Path(store_path)
        self._fingerprints: Dict[str, DataFingerprint] = {}
        self._lock = threading.Lock()
        self._load()

    # ── 基础读写 ──

    def get(self, symbol: str) -> Optional[DataFingerprint]:
        """获取指定标的的指纹, 不存在返回 None。

        Args:
            symbol: 标的代码。

        Returns:
            DataFingerprint 或 None。
        """
        with self._lock:
            return self._fingerprints.get(symbol)

    def put(self, fp: DataFingerprint) -> None:
        """存入/更新一个指纹 (不自动持久化, 需调用 save())。

        Args:
            fp: 数据指纹。
        """
        with self._lock:
            self._fingerprints[fp.symbol] = fp

    def remove(self, symbol: str) -> bool:
        """删除指定标的的指纹。

        Args:
            symbol: 标的代码。

        Returns:
            True 表示存在并已删除; False 表示不存在。
        """
        with self._lock:
            if symbol in self._fingerprints:
                del self._fingerprints[symbol]
                return True
            return False

    def has(self, symbol: str) -> bool:
        """判断指定标的是否有指纹记录。

        Args:
            symbol: 标的代码。

        Returns:
            True 表示有记录。
        """
        with self._lock:
            return symbol in self._fingerprints

    def all_symbols(self) -> List[str]:
        """返回所有已记录标的代码 (排序后)。

        Returns:
            标的代码列表。
        """
        with self._lock:
            return sorted(self._fingerprints.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._fingerprints)

    def __contains__(self, symbol: str) -> bool:
        return self.has(symbol)

    # ── 变更检测 ──

    def has_changed(self, symbol: str, new_fp: DataFingerprint) -> bool:
        """判断指定标的的数据是否发生变更。

        Args:
            symbol: 标的代码。
            new_fp: 新计算的指纹。

        Returns:
            True 表示数据变更 (或该标的无历史指纹, 视为新增);
            False 表示未变更。
        """
        with self._lock:
            old_fp = self._fingerprints.get(symbol)
            if old_fp is None:
                return True
            return old_fp != new_fp

    def get_changed_symbols(
        self, new_fingerprints: Dict[str, DataFingerprint]
    ) -> List[str]:
        """对比新旧指纹, 返回发生变更的标的列表。

        变更包括:
          - 新增标的 (无历史指纹)
          - 数据内容变化 (hash_value 不同)
          - 数据结构变化 (columns / row_count / date_range 不同)

        已删除的标的 (有历史指纹但新指纹中不存在) 不计入变更
        (增量回测场景下, 删除标的不需要重算)。

        Args:
            new_fingerprints: {symbol: DataFingerprint} 新指纹字典。

        Returns:
            变更标的代码列表 (排序后)。
        """
        with self._lock:
            changed: List[str] = []
            for symbol, new_fp in new_fingerprints.items():
                old_fp = self._fingerprints.get(symbol)
                if old_fp is None or old_fp != new_fp:
                    changed.append(symbol)
            return sorted(changed)

    def sync(self, new_fingerprints: Dict[str, DataFingerprint]) -> List[str]:
        """用新指纹批量更新仓库, 并返回变更标的列表。

        此方法会:
          1. 对比新旧指纹, 识别变更标的
          2. 用新指纹全量覆盖仓库
          3. 原子持久化到磁盘

        Args:
            new_fingerprints: {symbol: DataFingerprint} 新指纹字典。

        Returns:
            变更标的代码列表 (排序后)。
        """
        with self._lock:
            changed: List[str] = []
            for symbol, new_fp in new_fingerprints.items():
                old_fp = self._fingerprints.get(symbol)
                if old_fp is None or old_fp != new_fp:
                    changed.append(symbol)

            # 全量替换
            self._fingerprints = dict(new_fingerprints)
            self._save_locked()

            logger.info(
                "指纹仓库同步完成: 总计 %d 个标的, 变更 %d 个",
                len(self._fingerprints), len(changed),
            )
            return sorted(changed)

    # ── 持久化 ──

    def save(self) -> None:
        """将当前指纹仓库持久化到磁盘 (原子写入)。"""
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        """内部保存方法 (调用方必须已持有 _lock)。"""
        data = {
            "format_version": FINGERPRINT_FORMAT_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "fingerprints": {
                symbol: fp.to_dict()
                for symbol, fp in self._fingerprints.items()
            },
        }
        _atomic_write_json(self._path, data)

    def _load(self) -> None:
        """从磁盘加载指纹仓库 (文件不存在则初始化为空)。"""
        if not self._path.exists():
            logger.debug("指纹仓库文件不存在, 初始化为空: %s", self._path)
            return

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(
                "指纹仓库 JSON 解析失败, 将初始化为空: %s, 错误: %s",
                self._path, e,
            )
            return

        fps_data = data.get("fingerprints", {})
        for symbol, fp_dict in fps_data.items():
            try:
                self._fingerprints[symbol] = DataFingerprint.from_dict(fp_dict)
            except (KeyError, ValueError) as e:
                logger.warning(
                    "跳过无效指纹记录: symbol=%s, 错误: %s", symbol, e,
                )

        logger.debug(
            "指纹仓库加载完成: %s, 共 %d 个标的",
            self._path, len(self._fingerprints),
        )

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"FingerprintStore(path='{self._path}', "
                f"count={len(self._fingerprints)})"
            )


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import shutil

    print("=" * 60)
    print("  data_fingerprint.py 自测 Demo")
    print("=" * 60)

    # 1. 构造示例行情数据
    print("\n[1] 构造示例行情数据 (5 条日线, DatetimeIndex)")
    dates = pd.date_range("2024-01-02", periods=5, freq="B")
    df = pd.DataFrame(
        {
            "open":   [10.0, 10.2, 10.6, 10.9, 11.1],
            "high":   [10.5, 10.8, 11.0, 11.2, 11.5],
            "low":    [9.8,  10.1, 10.5, 10.8, 11.0],
            "close":  [10.2, 10.6, 10.9, 11.1, 11.3],
            "volume": [100000, 120000, 110000, 95000, 105000],
        },
        index=dates,
    )
    df.index.name = "date"
    print(f"    数据形状: {df.shape}, 索引类型: {type(df.index).__name__}")

    # 2. 计算指纹
    print("\n[2] 计算数据指纹")
    fp1 = compute_fingerprint(df, symbol="600519.SH", data_source="demo")
    print(f"    {fp1}")
    print(f"    完整 hash: {fp1.hash_value}")
    print(f"    日期范围: {fp1.start_date} ~ {fp1.end_date}")
    print(f"    列名: {fp1.columns}")

    # 3. 确定性验证: 相同数据应产生相同指纹
    print("\n[3] 确定性验证 (相同数据 → 相同指纹)")
    fp2 = compute_fingerprint(df, symbol="600519.SH", data_source="demo")
    assert fp1 == fp2, "相同数据应产生相同指纹!"
    assert fingerprints_equal(fp1, fp2)
    print(f"    ✓ 两次计算指纹一致: {fp1.hash_value[:16]}...")

    # 4. 行顺序无关性验证
    print("\n[4] 行顺序无关性验证 (打乱行序 → 指纹不变)")
    df_shuffled = df.sample(frac=1.0, random_state=42)
    fp_shuffled = compute_fingerprint(df_shuffled, symbol="600519.SH")
    assert fp1 == fp_shuffled, "行顺序不应影响指纹!"
    print(f"    ✓ 打乱行序后指纹不变")

    # 5. 列顺序无关性验证
    print("\n[5] 列顺序无关性验证 (重排列 → 指纹不变)")
    cols_reversed = list(reversed(df.columns))
    df_reordered = df[cols_reversed]
    fp_reordered = compute_fingerprint(df_reordered, symbol="600519.SH")
    assert fp1 == fp_reordered, "列顺序不应影响指纹!"
    print(f"    ✓ 重排列后指纹不变")

    # 6. 数据变更检测
    print("\n[6] 数据变更检测 (修改收盘价 → 指纹变化)")
    df_modified = df.copy()
    df_modified.iloc[0, df_modified.columns.get_loc("close")] = 999.0
    fp_modified = compute_fingerprint(df_modified, symbol="600519.SH")
    assert fp1 != fp_modified, "数据变更应产生不同指纹!"
    print(f"    原指纹: {fp1.hash_value[:16]}...")
    print(f"    新指纹: {fp_modified.hash_value[:16]}...")
    print(f"    ✓ 数据变更被正确检测")

    # 7. 空 DataFrame 处理
    print("\n[7] 空 DataFrame 处理")
    df_empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    fp_empty = compute_fingerprint(df_empty, symbol="EMPTY")
    print(f"    {fp_empty}")
    assert fp_empty.row_count == 0
    assert fp_empty.start_date == "" and fp_empty.end_date == ""
    print(f"    ✓ 空 DataFrame 指纹正常")

    # 8. 字符串日期列处理
    print("\n[8] 字符串日期列处理 (date 列为字符串)")
    df_str_date = df.reset_index()
    df_str_date["date"] = df_str_date["date"].dt.strftime("%Y-%m-%d")
    fp_str = compute_fingerprint(df_str_date, symbol="600519.SH")
    print(f"    日期范围: {fp_str.start_date} ~ {fp_str.end_date}")
    assert fp_str.start_date == "2024-01-02"
    assert fp_str.end_date == "2024-01-08"
    print(f"    ✓ 字符串日期列正确识别")

    # 9. 指纹持久化 (单个)
    print("\n[9] 指纹持久化 (保存 / 加载)")
    tmp_dir = Path(tempfile.mkdtemp(prefix="fp_test_"))
    fp_file = tmp_dir / "600519_fingerprint.json"
    save_fingerprint(fp1, fp_file)
    fp_loaded = load_fingerprint(fp_file)
    assert fp1 == fp_loaded, "保存后加载的指纹应与原指纹一致!"
    print(f"    ✓ 保存/加载往返一致")

    # 10. FingerprintStore 多标的仓库
    print("\n[10] FingerprintStore 多标的仓库")
    store_path = tmp_dir / "fingerprint_store.json"
    store = FingerprintStore(store_path)

    fp_a = compute_fingerprint(df, symbol="600519.SH")
    df_b = df * 1.5  # 另一个标的的数据
    fp_b = compute_fingerprint(df_b, symbol="000001.SZ")
    store.put(fp_a)
    store.put(fp_b)
    store.save()
    print(f"    存入 2 个标的: {store.all_symbols()}")
    assert len(store) == 2
    assert "600519.SH" in store
    assert store.get("600519.SH") == fp_a

    # 11. 变更检测
    print("\n[11] 变更检测 (修改一个标的的数据)")
    df_a_modified = df.copy()
    df_a_modified.iloc[-1, df_a_modified.columns.get_loc("close")] = 888.0
    fp_a_new = compute_fingerprint(df_a_modified, symbol="600519.SH")
    fp_b_unchanged = compute_fingerprint(df_b, symbol="000001.SZ")

    new_fps = {"600519.SH": fp_a_new, "000001.SZ": fp_b_unchanged}
    changed = store.get_changed_symbols(new_fps)
    print(f"    变更标的: {changed}")
    assert changed == ["600519.SH"], "只有 600519.SH 应被检测为变更!"
    print(f"    ✓ 变更检测正确")

    # 12. sync 批量同步 + 持久化验证
    print("\n[12] sync 批量同步 (全量覆盖 + 原子写入)")
    changed_after_sync = store.sync(new_fps)
    print(f"    sync 返回变更: {changed_after_sync}")
    assert changed_after_sync == ["600519.SH"]

    # 重新加载验证持久化
    store2 = FingerprintStore(store_path)
    assert len(store2) == 2
    assert store2.get("600519.SH") == fp_a_new
    print(f"    ✓ sync 后重新加载, 数据一致")

    # 13. 新增标的检测
    print("\n[13] 新增标的检测")
    df_c = df * 2.0
    fp_c = compute_fingerprint(df_c, symbol="300750.SZ")
    new_fps2 = {
        "600519.SH": fp_a_new,
        "000001.SZ": fp_b_unchanged,
        "300750.SZ": fp_c,
    }
    changed2 = store.get_changed_symbols(new_fps2)
    print(f"    变更标的 (含新增): {changed2}")
    assert "300750.SZ" in changed2, "新增标的应被检测为变更!"
    print(f"    ✓ 新增标的检测正确")

    # 14. 输入校验
    print("\n[14] 输入校验 (非法参数)")
    try:
        compute_fingerprint(df, symbol="")
        assert False, "应抛出 ValueError"
    except ValueError:
        print(f"    ✓ 空 symbol 抛出 ValueError")

    try:
        compute_fingerprint("not_a_dataframe", symbol="600519.SH")  # type: ignore
        assert False, "应抛出 TypeError"
    except TypeError:
        print(f"    ✓ 非 DataFrame 输入抛出 TypeError")

    # 清理
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"\n[15] 临时目录已清理: {tmp_dir}")

    print("\n" + "=" * 60)
    print("  ✓ data_fingerprint.py 全部自测通过")
    print("=" * 60)
