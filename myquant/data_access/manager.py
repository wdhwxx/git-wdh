# 文件名：myquant/data_access/manager.py
"""
manager.py - 数据管理器（更新/校验/同步/自动维护）
===================================================
数据层的管理入口，负责数据的自动更新、质量校验、同步状态管理。

核心能力:
  1. 增量更新：只更新最新数据，不全量重下
  2. 数据校验：完整性检查/异常检测/双源对比
  3. 同步状态：记录每只股票最后更新时间
  4. 自动同步：盘后自动更新所有股票数据
  5. 数据修复：检测到异常数据时自动修复
  6. 统计报告：数据覆盖率/更新状态/质量报告

设计原则:
  - 防未来函数：更新时严格按当前日期，不获取未来数据
  - 增量优先：只更新缺失的数据，不重复下载
  - 事务安全：所有写入在事务中执行，失败自动回滚
  - 可追溯：所有更新操作记录日志和状态
  - 为实盘预留：盘中实时监控数据延迟

依赖: typing, datetime, threading, myquant.database, myquant.data_access.fetcher
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from myquant.core.logger import get_logger

logger = get_logger("data_access.manager")

from .fetcher import DataFetcher
from .sources.base import MarketDataResult
from .cleaner import load_clean_config, run_clean_pipeline
from myquant.utils.file_utils import (
    ensure_dir, get_project_path, write_csv, read_csv,
    read_json, write_json, file_sha256, file_lock,
)


# ─────────────────────────────────────────────
# 常量定义
# ─────────────────────────────────────────────

# 默认更新批量大小
DEFAULT_BATCH_SIZE = 50

# 数据异常阈值
PRICE_JUMP_THRESHOLD = 0.5  # 单日涨跌幅超过50%视为异常（除权除息除外）
VOLUME_ZERO_THRESHOLD = 0   # 成交量为0视为停牌或异常

# 全量 processed 扁平 parquet 固定列模板（与 scripts/数据导出_DB到processed.py
# load_symbol_data 导出一致：日期 + 13 业务列；无财务数据时 B 类 4 列置 None）。
# 增量"无存量新建"分支必须对齐本模板，保证增量新建文件与全量导出 schema 完全一致。
PROCESSED_COLUMN_TEMPLATE: List[str] = [
    "日期", "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额",
    "涨跌幅", "振幅", "量比", "换手率", "流通股本", "市盈动", "每股收益",
]


# ─────────────────────────────────────────────
# 数据集版本异常 (任务14新增)
# ─────────────────────────────────────────────

class DatasetVersionError(Exception):
    """数据集版本管理基础异常"""
    pass


class DatasetVersionNotFoundError(DatasetVersionError):
    """版本不存在"""
    pass


class DataVersionIntegrityError(DatasetVersionError):
    """版本数据完整性校验失败（checksum不匹配）"""
    pass


def _normalize_processed_df(df: "pd.DataFrame") -> "pd.DataFrame":
    """归一化 processed 存量：确保含整数「日期」列（兼容旧索引式/扁平 datetime64）。

    旧索引式（DatetimeIndex、无「日期」列）→ reset_index 为「日期」列；
    「日期」列 datetime64 → 转整数 YYYYMMDD（与 _get_processed_last_date 读取口径一致）。
    无法恢复日期（无日期列且非 DatetimeIndex）时原样返回，由调用方跳过合并。
    """
    if df is None or df.empty:
        return df
    if "日期" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index()
            if df.columns[0] != "日期":
                df = df.rename(columns={df.columns[0]: "日期"})
        else:
            return df
    if "日期" in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df["日期"]):
            df["日期"] = df["日期"].dt.strftime("%Y%m%d").astype(int)
        else:
            df["日期"] = pd.to_numeric(
                df["日期"].astype(str).str.replace("-", ""), errors="coerce"
            )
    return df


def _verify_existing_unchanged(
    existing: "pd.DataFrame",
    merged: "pd.DataFrame",
    symbol: str,
) -> None:
    """存量保护校验：合并结果中存量部分必须与读入时逐值等价（硬约束）。

    比较口径：存量日期集合在 merged 中的行（按日期升序）vs 读入时存量，
    逐列逐值比较（NaN 视为相等，数值误差 >1e-8 即判定不等）。
    任何不等抛 RuntimeError——调用方捕获后记入失败清单、不写出 processed，
    保证增量合并绝不修改存量表头下的任何数据。
    """
    existing_dates = set(int(d) for d in existing["日期"].dropna())
    if not existing_dates:
        return
    merged_mask = merged["日期"].astype(float).isin([float(d) for d in existing_dates])
    merged_existing = merged[merged_mask].sort_values("日期").reset_index(drop=True)
    ref = existing.sort_values("日期").reset_index(drop=True)
    if len(merged_existing) != len(ref):
        raise RuntimeError(
            f"存量保护校验失败: 存量行数变化 {len(ref)} → {len(merged_existing)} ({symbol})"
        )
    cols = [c for c in ref.columns if c in merged_existing.columns]
    for c in cols:
        a = pd.to_numeric(ref[c], errors="coerce")
        b = pd.to_numeric(merged_existing[c], errors="coerce")
        both_nan = a.isna() & b.isna()
        diff = (a - b).abs()
        bad = (diff > 1e-8) & ~both_nan
        if bad.any():
            idx = int(bad.idxmax())
            raise RuntimeError(
                f"存量保护校验失败: 列「{c}」第 {idx} 行数值不等 "
                f"(存量 {a.iloc[idx]!r} vs 合并后 {b.iloc[idx]!r}) ({symbol})"
            )


# ─────────────────────────────────────────────
# DatasetVersion 版本元数据 (任务14新增)
# ─────────────────────────────────────────────

@dataclass
class DatasetVersion:
    """
    数据集版本元数据（任务14）

    记录一次数据集快照的完整元信息，供版本管理/追溯/回滚使用。

    Attributes
    ----------
    version : int
        版本号（同一数据集的版本单调递增）
    symbol : str
        标的代码
    data_type : str
        数据类型：bar/tick（对应 DataType.value）
    period : str
        K线周期：daily/minute/tick
    adjust : str | None
        复权方式：raw（当前库内不复权原始层）/hfq/qfq（预留）
    created_at : str
        版本创建时间（UTC，YYYY-MM-DD HH:MM:SS）
    source : str
        数据来源：offline/live（实盘预留）
    record_count : int
        快照数据条数
    total / valid / bad : int
        清洗统计（原始总数/有效数/脏数据数）
    valid_rate : float | None
        合格率
    quality_grade : str | None
        质量等级（QualityGrade.value）
    checksum : str | None
        数据快照SHA256（完整性校验）
    data_range : dict | None
        数据时间范围 {start_date, end_date}
    clean_run_id : str | None
        关联任务13 clean_run_meta.run_id（清洗运行追溯）
    parent_version : int | None
        派生链上一版本（实盘增量版本）
    description : str
        版本说明
    metadata : dict
        扩展元数据
    """
    version: int
    symbol: str
    data_type: str = "bar"
    period: str = "daily"
    adjust: Optional[str] = "raw"
    created_at: Optional[str] = None
    source: str = "offline"
    record_count: int = 0
    total: int = 0
    valid: int = 0
    bad: int = 0
    valid_rate: Optional[float] = None
    quality_grade: Optional[str] = None
    checksum: Optional[str] = None
    data_range: Optional[Dict[str, str]] = None
    clean_run_id: Optional[str] = None
    parent_version: Optional[int] = None
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（JSON可存储）"""
        return {
            "version": self.version,
            "symbol": self.symbol,
            "data_type": self.data_type,
            "period": self.period,
            "adjust": self.adjust,
            "created_at": self.created_at,
            "source": self.source,
            "record_count": self.record_count,
            "total": self.total,
            "valid": self.valid,
            "bad": self.bad,
            "valid_rate": self.valid_rate,
            "quality_grade": self.quality_grade,
            "checksum": self.checksum,
            "data_range": self.data_range,
            "clean_run_id": self.clean_run_id,
            "parent_version": self.parent_version,
            "description": self.description,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DatasetVersion":
        """从字典反序列化"""
        return cls(
            version=data.get("version", 0),
            symbol=data.get("symbol", ""),
            data_type=data.get("data_type", "bar"),
            period=data.get("period", "daily"),
            adjust=data.get("adjust", "raw"),
            created_at=data.get("created_at"),
            source=data.get("source", "offline"),
            record_count=data.get("record_count", 0),
            total=data.get("total", 0),
            valid=data.get("valid", 0),
            bad=data.get("bad", 0),
            valid_rate=data.get("valid_rate"),
            quality_grade=data.get("quality_grade"),
            checksum=data.get("checksum"),
            data_range=data.get("data_range"),
            clean_run_id=data.get("clean_run_id"),
            parent_version=data.get("parent_version"),
            description=data.get("description", ""),
            metadata=data.get("metadata", {}) or {},
        )

    @property
    def data_file_name(self) -> str:
        """数据快照文件名（含 data_type/period/adjust 标识）"""
        adjust_suffix = f"_{self.adjust}" if self.adjust else ""
        return f"{self.data_type}_{self.period}{adjust_suffix}.csv"


# ─────────────────────────────────────────────
# 数据管理器
# ─────────────────────────────────────────────

class DataManager:
    """
    数据管理器
    ==========
    负责数据的自动更新、质量校验、同步状态管理。

    Usage:
        from myquant.data_access import DataFetcher, DataManager
        from myquant.data_access.sources.tdx_source import TdxDataSource

        fetcher = DataFetcher()
        fetcher.add_source(TdxDataSource())
        manager = DataManager(fetcher)

        # 增量更新单只股票
        manager.update_symbol("000001.SZ")

        # 批量更新所有股票
        manager.update_all_symbols()

        # 数据质量校验
        report = manager.validate_data("000001.SZ")
    """

    def __init__(
        self,
        fetcher: DataFetcher,
        db_connection=None,
    ):
        """
        Parameters
        ----------
        fetcher : DataFetcher
            数据获取器实例
        db_connection : DatabaseConnection | None
            数据库连接（用于持久化同步状态），None则仅内存管理
        """
        self._fetcher = fetcher
        self._db = db_connection
        self._lock = threading.Lock()
        self._sync_status: Dict[str, Dict] = {}  # 内存同步状态缓存
        self._update_stats: Dict[str, Any] = {
            "total_updated": 0,
            "total_failed": 0,
            "last_update_time": None,
        }

    # ── 属性 ──

    @property
    def fetcher(self) -> DataFetcher:
        return self._fetcher

    @property
    def update_stats(self) -> Dict:
        return dict(self._update_stats)

    # ═══════════════════════════════════════════
    # 增量更新
    # ═══════════════════════════════════════════

    def update_symbol(
        self,
        symbol: str,
        period: str = "daily",
        adjust: Optional[str] = "raw",
        force_full: bool = False,
    ) -> Dict:
        """
        增量更新单只股票数据

        【防未来函数】只更新到当前日期，不获取未来数据

        Parameters
        ----------
        symbol : str
            股票代码
        period : str
            周期
        adjust : str | None
            复权方式
        force_full : bool
            是否强制全量更新（忽略已有数据）

        Returns
        -------
        dict : 更新结果 {
            symbol, status, record_count, start_date, end_date,
            data_source, error_message
        }
        """
        today = datetime.now().strftime("%Y-%m-%d")
        result = {
            "symbol": symbol,
            "status": "pending",
            "record_count": 0,
            "start_date": None,
            "end_date": None,
            "data_source": "",
            "error_message": "",
            # M1修复：携带已拉取记录，供 save_cleaned_incremental_parquet 复用，消除增量双拉取
            "data": None,
        }

        try:
            # 确定起始日期
            start_date = None
            if not force_full:
                # M5修复：增量起点以 processed 实际最新交易日为准（回测/实盘消费的权威数据）；
                # sync_status 仅代表"曾拉取成功"，processed 写回失败时 DB 领先，
                # 若以 DB 为准会跳过缺失区间造成死锁。processed 缺失时回退 DB。
                last_date = self._get_processed_last_date(symbol, period) or self._get_last_sync_date(symbol, period)
                if last_date:
                    # 从最后同步日期的下一天开始
                    last_dt = datetime.strptime(last_date, "%Y-%m-%d")
                    start_date = (last_dt + timedelta(days=1)).strftime("%Y-%m-%d")
                    # 如果已经是最新，跳过
                    if start_date > today:
                        result["status"] = "skipped"
                        result["end_date"] = last_date
                        return result

            # 获取数据（绕过缓存，确保最新）
            data_result = self._fetcher.get_market_data(
                symbol, start_date=start_date, end_date=today,
                period=period, adjust=adjust, use_cache=False,
            )

            if data_result and data_result.count > 0:
                result["status"] = "completed"
                result["record_count"] = data_result.count
                result["start_date"] = data_result.start_date
                result["end_date"] = data_result.end_date
                result["data_source"] = data_result.source
                # M1修复：记录写入 result 供清洗复用（增量区间仅1~3根，内存开销可忽略）
                result["data"] = list(data_result.data)

                # 持久化到数据库（如果有连接）
                if self._db:
                    self._save_to_database(symbol, data_result, period, adjust)

                # 更新同步状态
                self._update_sync_status(
                    symbol, period, "market",
                    data_result.end_date, data_result.count,
                    "completed", data_result.source,
                )
                # 使缓存失效
                self._fetcher.invalidate_cache(symbol)
                self._update_stats["total_updated"] += 1
            else:
                result["status"] = "no_data"
                result["error_message"] = "未获取到数据"
                # 缺口可见：记录请求区间（processed/sync 最新+1 → today），
                # 供 GUI 展示缺口（休市/停牌/数据源缺失由用户结合日期判断）
                result["start_date"] = start_date
                result["end_date"] = today
                self._update_stats["total_failed"] += 1

        except Exception as e:
            result["status"] = "failed"
            result["error_message"] = str(e)
            self._update_sync_status(
                symbol, period, "market",
                None, 0, "failed", "", str(e),
            )
            self._update_stats["total_failed"] += 1

        self._update_stats["last_update_time"] = datetime.now().isoformat()
        return result

    def update_symbols_batch(
        self,
        symbols: List[str],
        period: str = "daily",
        adjust: Optional[str] = "raw",
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress_callback: Optional[Callable] = None,
        force_full: bool = False,
        batch_sleep: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> List[Dict]:
        """
        批量更新股票数据

        Parameters
        ----------
        symbols : list[str]
            股票代码列表
        period : str
            周期
        adjust : str | None
            复权方式
        batch_size : int
            每批处理数量
        progress_callback : callable | None
            进度回调函数 callback(current, total, result)
        force_full : bool
            是否强制全量重下（忽略数据库已同步状态，从头拉取全部历史）。
            True=全量重下（首次部署/数据修复/手动重建）；
            False=增量（只补缺失，默认）。
            增量/全量共用本函数：DataManager 依据数据库 sync_status 自动判断，
            force_full=True 时跳过增量判断直接全量拉取，strictly end_date=today 防未来函数。
        batch_sleep : float | None
            批间限速间隔（秒）。None 时从 config/data_clean.yaml 的 incremental.batch_sleep 读取，
            未配置则 0（本地通达信源零限流，可不等待）。
        max_retries : int | None
            失败（failed/no_data）退避重试次数。None 时从 config/data_clean.yaml 的 incremental.max_retries 读取，
            未配置则 0（不重试）。

        Returns
        -------
        list[dict] : 所有股票更新结果
        """
        results = []
        total = len(symbols)
        # M2修复：限速/重试参数配置外置（config/data_clean.yaml 的 incremental 段），未配置则不生效
        if batch_sleep is None or max_retries is None:
            _inc = (load_clean_config().get("incremental") or {})
            if batch_sleep is None:
                batch_sleep = float(_inc.get("batch_sleep", 0.0))
            if max_retries is None:
                max_retries = int(_inc.get("max_retries", 0))
        batch_sleep = float(batch_sleep or 0.0)
        max_retries = max(0, int(max_retries or 0))
        for i, symbol in enumerate(symbols):
            result = self.update_symbol(symbol, period, adjust, force_full=force_full)
            # M2修复：失败退避重试（网络兜底防限流；本地通达信成功则零开销）
            if max_retries and result.get("status") in ("failed", "no_data"):
                for _ in range(max_retries):
                    time.sleep(batch_sleep if batch_sleep > 0 else 0.3)
                    retry = self.update_symbol(symbol, period, adjust, force_full=force_full)
                    if retry.get("status") == "completed":
                        result = retry
                        break
            results.append(result)
            if progress_callback:
                try:
                    progress_callback(i + 1, total, result)
                except Exception as e:
                    logger.debug(f"进度回调异常: {e}")
            # M2修复：按批限速（网络兜底防限流；本地源 batch_sleep 可配为 0 不等待）
            if batch_sleep > 0 and (i + 1) % batch_size == 0 and i + 1 < total:
                time.sleep(batch_sleep)
        return results

    def update_all_symbols(
        self,
        market: str = "A股",
        period: str = "daily",
        adjust: Optional[str] = "raw",
        progress_callback: Optional[Callable] = None,
        force_full: bool = False,
    ) -> List[Dict]:
        """
        更新所有股票数据

        Parameters
        ----------
        market : str
            市场
        period : str
            周期
        adjust : str | None
            复权方式
        progress_callback : callable | None
            进度回调

        Returns
        -------
        list[dict] : 更新结果
        """
        stocks = self._fetcher.get_stock_list(market)
        symbols = [s["symbol"] for s in stocks if s.get("symbol")]
        return self.update_symbols_batch(
            symbols, period, adjust,
            progress_callback=progress_callback, force_full=force_full,
        )

    # ═══════════════════════════════════════════
    # 数据质量校验
    # ═══════════════════════════════════════════

    def validate_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
    ) -> Dict:
        """
        数据质量校验

        检查项:
        1. 数据完整性：是否有缺失交易日
        2. 价格异常：单日涨跌幅超过阈值
        3. 成交量异常：成交量为0
        4. 数据连续性：日期是否连续

        【防未来函数】end_date 之后的数据不检查

        Parameters
        ----------
        symbol : str
            股票代码
        start_date : str | None
            开始日期
        end_date : str | None
            截止日期

        Returns
        -------
        dict : 校验报告 {
            symbol, total_records, missing_dates, price_anomalies,
            volume_anomalies, continuity_issues, overall_status
        }
        """
        data_result = self._fetcher.get_market_data(
            symbol, start_date, end_date, period
        )
        report = {
            "symbol": symbol,
            "total_records": data_result.count if data_result else 0,
            "missing_dates": [],
            "price_anomalies": [],
            "volume_anomalies": [],
            "continuity_issues": [],
            "overall_status": "unknown",
        }

        if not data_result or data_result.count == 0:
            report["overall_status"] = "no_data"
            return report

        data = data_result.data
        prev_close = None

        for i, row in enumerate(data):
            date = row.get("date", "")
            close = row.get("close")
            volume = row.get("volume", 0)
            pct_chg = row.get("pct_chg")

            # 成交量异常
            if volume is not None and volume <= VOLUME_ZERO_THRESHOLD:
                report["volume_anomalies"].append({
                    "date": date, "volume": volume,
                    "reason": "成交量为0",
                })

            # 价格异常（涨跌幅超过阈值）
            if pct_chg is not None and abs(pct_chg) > PRICE_JUMP_THRESHOLD * 100:
                report["price_anomalies"].append({
                    "date": date, "pct_chg": pct_chg,
                    "reason": f"涨跌幅{pct_chg:.2f}%超过阈值{PRICE_JUMP_THRESHOLD*100:.0f}%",
                })

            # 数据连续性检查（交易日之间不应超过5天，除节假日外）
            if i > 0:
                prev_date = data[i - 1].get("date", "")
                try:
                    d1 = datetime.strptime(prev_date, "%Y-%m-%d")
                    d2 = datetime.strptime(date, "%Y-%m-%d")
                    gap = (d2 - d1).days
                    if gap > 7:  # 超过7天可能有缺失
                        report["continuity_issues"].append({
                            "prev_date": prev_date,
                            "curr_date": date,
                            "gap_days": gap,
                            "reason": f"日期间隔{gap}天，可能有缺失",
                        })
                except ValueError:
                    pass

            prev_close = close

        # 总体状态判断
        issue_count = (
            len(report["missing_dates"])
            + len(report["price_anomalies"])
            + len(report["volume_anomalies"])
            + len(report["continuity_issues"])
        )
        if issue_count == 0:
            report["overall_status"] = "healthy"
        elif issue_count < 5:
            report["overall_status"] = "minor_issues"
        else:
            report["overall_status"] = "needs_repair"

        return report

    def validate_batch(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """批量校验多只股票数据质量"""
        return [self.validate_data(s, start_date, end_date) for s in symbols]

    # ═══════════════════════════════════════════
    # 同步状态管理
    # ═══════════════════════════════════════════

    def _get_processed_last_date(
        self,
        symbol: str,
        period: str = "daily",
    ) -> Optional[str]:
        """读取 processed 扁平 parquet 的实际最新交易日（YYYY-MM-DD）。

        M5修复：processed 是回测/实盘消费的权威数据，增量起点必须以其实际
        最新交易日为准；sync_status 只代表"曾拉取成功"，processed 写回失败时
        DB 领先会导致缺失区间被永久跳过（死锁）。只读存量历史文件，
        不获取任何未来数据（防未来函数）。

        Args:
            symbol: 标的代码，如 000001.SZ。
            period: 周期（当前仅 daily 扁平 parquet）。

        Returns:
            Optional[str]: 最新交易日 YYYY-MM-DD；文件缺失/损坏/无日期列/
            日期列全空时返回 None（调用方回退 sync_status 或全量拉取）。
        """
        if period != "daily":
            return None
        processed_dir = get_project_path("datasets", "processed")
        path = processed_dir / f"{symbol}.parquet"
        if not path.exists():
            return None
        try:
            import pandas as pd

            df = pd.read_parquet(path, columns=["日期"])
            if df is None or df.empty or "日期" not in df.columns:
                return None
            vals = pd.to_numeric(df["日期"], errors="coerce").dropna()
            if vals.empty:
                return None
            d = int(vals.max())
            return f"{d // 10000:04d}-{d % 10000 // 100:02d}-{d % 100:02d}"
        except Exception as e:
            # 读取失败不阻断增量链路，回退 sync_status 判断
            logger.debug("processed 最新日期读取失败，回退 sync_status: %s (%s)", symbol, e)
            return None

    def _get_last_sync_date(
        self,
        symbol: str,
        period: str = "daily",
        data_type: str = "market",
    ) -> Optional[str]:
        """获取最后同步日期"""
        key = f"{symbol}|{period}|{data_type}"
        # 先查内存
        if key in self._sync_status:
            return self._sync_status[key].get("last_sync_date")
        # 再查数据库
        if self._db:
            try:
                from ..database.queries import DataQueries
                queries = DataQueries(self._db)
                status_list = queries.get_sync_status(symbol=symbol)
                for s in status_list:
                    if s.get("period") == period and s.get("data_type") == data_type:
                        self._sync_status[key] = s
                        return s.get("last_sync_date")
            except Exception as e:
                logger.debug(f"同步状态查询失败: symbol={symbol}, 错误: {e}")
        return None

    def _update_sync_status(
        self,
        symbol: str,
        period: str,
        data_type: str,
        last_sync_date: Optional[str],
        record_count: int,
        status: str,
        data_source: str,
        error_message: str = "",
    ) -> None:
        """更新同步状态"""
        key = f"{symbol}|{period}|{data_type}"
        self._sync_status[key] = {
            "symbol": symbol,
            "period": period,
            "data_type": data_type,
            "last_sync_date": last_sync_date,
            "record_count": record_count,
            "sync_status": status,
            "data_source": data_source,
            "error_message": error_message,
        }
        # 持久化到数据库
        if self._db:
            try:
                from ..database.queries import DataQueries
                queries = DataQueries(self._db)
                queries.update_sync_status(
                    symbol, period, data_type,
                    last_sync_date or "", record_count,
                    status, data_source, error_message,
                )
            except Exception as e:
                logger.warning(f"同步状态更新失败: symbol={symbol}, period={period}, 错误: {e}")

    def get_sync_report(self) -> Dict:
        """获取同步状态报告"""
        total = len(self._sync_status)
        completed = sum(1 for s in self._sync_status.values() if s.get("sync_status") == "completed")
        failed = sum(1 for s in self._sync_status.values() if s.get("sync_status") == "failed")
        pending = sum(1 for s in self._sync_status.values() if s.get("sync_status") == "pending")
        return {
            "total_symbols": total,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "update_stats": self._update_stats,
        }

    # ═══════════════════════════════════════════
    # 数据库持久化
    # ═══════════════════════════════════════════

    def _save_to_database(
        self,
        symbol: str,
        data_result: MarketDataResult,
        period: str,
        adjust: Optional[str],
    ) -> int:
        """
        将行情数据保存到数据库

        Returns
        -------
        int : 保存的记录数
        """
        if not self._db or not data_result or data_result.count == 0:
            return 0
        try:
            from ..database.queries import DataQueries
            queries = DataQueries(self._db)
            # 转换为数据库格式
            db_data = []
            for row in data_result.data:
                db_data.append({
                    "symbol": symbol,
                    "market": data_result.market,
                    "period": period,
                    "adjust": adjust,
                    "trade_date": row.get("date", ""),
                    "trade_time": row.get("time"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                    "amount": row.get("amount"),
                    "pct_chg": row.get("pct_chg"),
                    "turnover": row.get("turnover"),
                    "is_suspended": 1 if row.get("volume", 0) == 0 else 0,
                    "data_source": data_result.source,
                })
            return queries.bulk_insert_market_data(db_data)
        except Exception:
            return 0

    # ═══════════════════════════════════════════
    # 自动同步（盘后更新）
    # ═══════════════════════════════════════════

    def auto_sync_after_market(
        self,
        market: str = "A股",
        period: str = "daily",
        adjust: Optional[str] = "raw",
        progress_callback: Optional[Callable] = None,
    ) -> Dict:
        """
        盘后自动同步所有股票数据

        【防未来函数】只更新到当前日期

        Parameters
        ----------
        market : str
            市场
        period : str
            周期
        adjust : str | None
            复权方式
        progress_callback : callable | None
            进度回调

        Returns
        -------
        dict : 同步结果汇总
        """
        results = self.update_all_symbols(market, period, adjust, progress_callback)
        success = sum(1 for r in results if r["status"] == "completed")
        failed = sum(1 for r in results if r["status"] == "failed")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        no_data = sum(1 for r in results if r["status"] == "no_data")
        return {
            "total": len(results),
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "no_data": no_data,
            "details": results,
        }

    # ═══════════════════════════════════════════
    # 数据清洗与 processed 数据管理 (阶段2 v1)
    # ═══════════════════════════════════════════

    def clean_and_save_symbol(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "raw",
        data_type: str = "bar",
        clean_config: Optional[Dict] = None,
        save_bad: bool = True,
        save_report: bool = True,
    ) -> Dict:
        """
        一键清洗并保存：拉取原始数据 -> 清洗 -> 保存到 datasets/processed/

        【防未来函数】end_date 之后的数据不获取、不清洗

        Parameters
        ----------
        symbol : str
            标的代码
        start_date : str | None
            开始日期
        end_date : str | None
            截止日期
        period : str
            K线周期
        adjust : str | None
            复权方式
        data_type : str
            数据类型: bar | tick
        clean_config : Dict | None
            清洗配置，None 则自动加载 config/data_clean.yaml 或默认
        save_bad : bool
            是否保存脏数据
        save_report : bool
            是否生成质量报告

        Returns
        -------
        dict : {
            symbol, status, raw_count, cleaned_count, bad_count,
            processed_file, bad_file, report_file, stats
        }
        """
        result = {
            "symbol": symbol,
            "status": "pending",
            "raw_count": 0,
            "cleaned_count": 0,
            "bad_count": 0,
            "processed_file": None,
            "bad_file": None,
            "report_file": None,
            "stats": {},
            "error_message": "",
        }

        try:
            # 1. 加载清洗配置
            if clean_config is None:
                clean_config = load_clean_config()

            if not clean_config.get("enabled", True):
                result["status"] = "clean_disabled"
                result["error_message"] = "清洗功能已在配置中关闭"
                return result

            # 2. 拉取原始数据 (绕过缓存)
            data_result = self._fetcher.get_market_data(
                symbol, start_date=start_date, end_date=end_date,
                period=period, adjust=adjust, use_cache=False,
            )

            if not data_result or data_result.count == 0:
                result["status"] = "no_data"
                result["error_message"] = "未获取到原始数据"
                return result

            raw_data = data_result.data
            result["raw_count"] = len(raw_data)

            # 3. 执行清洗流水线
            clean_result = run_clean_pipeline(
                records=raw_data,
                data_type=data_type,
                symbol=symbol,
                config=clean_config,
                save_bad=save_bad,
                save_report=save_report,
            )

            cleaned = clean_result["cleaned"]
            bad = clean_result["bad"]
            stats = clean_result["stats"]

            result["cleaned_count"] = len(cleaned)
            result["bad_count"] = len(bad)
            result["bad_file"] = clean_result["bad_file"]
            result["report_file"] = clean_result["report_file"]
            result["stats"] = stats

            # 4. 保存清洗后数据到 processed
            # M2修复：启用跨批次去重（#O3修复声明的能力在此调用路径生效）
            if cleaned:
                processed_file = self._save_processed_data(
                    records=cleaned,
                    symbol=symbol,
                    data_type=data_type,
                    period=period,
                    adjust=adjust,
                    deduplicate_with_latest=True,
                )
                result["processed_file"] = processed_file

            result["status"] = "completed"
            self._update_stats["total_updated"] += 1

        except Exception as e:
            result["status"] = "failed"
            result["error_message"] = str(e)
            self._update_stats["total_failed"] += 1

        return result

    def _read_db_prior_bars(
        self,
        symbol: str,
        start_date: str,
        period: str = "daily",
        adjust: Optional[str] = "raw",
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        从 quant.db 读取 < start_date 的最近 limit 根 bar，作增量清洗上下文。

        M3修复：DB 是增量写入权威（quant.db 往往领先 processed），
        优先用 DB 保证跳变检测基准正确；processed 落后时不再作为唯一上下文来源。
        M6扩展：量比需前 5 日均量基准（VOLUME_RATIO_WINDOW），上下文从单根前收盘
        扩展为前 N 根（含前收盘 + 成交量均量窗口），与全量清洗 compute_derived_metrics 口径一致。

        【防未来函数】只读 start_date 之前的已发生数据，绝不读取未来。

        Args:
            symbol: 标的代码，如 000001.SZ。
            start_date: 增量区间开始日期（YYYY-MM-DD）。
            period / adjust: 查询口径，与增量拉取一致。
            limit: 取最近多少根（默认 5 = 量比窗口）。

        Returns:
            List[Dict]: 按日期升序的最近 limit 根行（英文字段 date/close/...），
                无数据或读取失败返回空列表。
        """
        if not self._db:
            return []
        try:
            from ..database.queries import DataQueries
            queries = DataQueries(self._db)
            prior_end = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            rows = queries.get_market_data(
                symbol, end_date=prior_end, period=period, adjust=adjust,
            )
            if not rows:
                return []
            rows = sorted(rows, key=lambda r: str(r.get("trade_date", "")))
            rows = rows[-limit:]
            # DB 行字段为 trade_date/open/close/...，转成清洗管线英文字段 date/close/...
            bars: List[Dict[str, Any]] = []
            for r in rows:
                en: Dict[str, Any] = {
                    "date": r.get("trade_date"),
                    "open": r.get("open"),
                    "high": r.get("high"),
                    "low": r.get("low"),
                    "close": r.get("close"),
                    "volume": r.get("volume"),
                    "amount": r.get("amount"),
                    "pct_chg": r.get("pct_chg"),
                    "turnover": r.get("turnover"),
                }
                bars.append({k: v for k, v in en.items() if v is not None})
            return bars
        except Exception as e:
            logger.warning("DB 前收盘上下文读取失败，回退 processed: %s: %s", symbol, e)
            return []

    def save_cleaned_incremental_parquet(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "raw",
        records: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """
        增量区间清洗后按中文列合并写回 processed 扁平 parquet。

        与全量清洗共用同一套规则（run_clean_pipeline + config/data_clean.yaml），
        仅处理有新增数据的标的与区间（带前 N 根上下文：前收盘 + 量比 5 日均量基准），
        规避全池全量慢扫，根治增量更新日线"直写原始数据弄脏 processed"的问题。
        清洗后与全量清洗同口径补派生量（A类：涨跌幅/振幅/量比；B类：换手率/流通股本/
        市盈动，B类与每股收益依赖 financial_data，DB 缺失或异常时跳过不阻断写入）。

        【防未来函数】end_date 之后的数据不获取、不清洗；只读存量历史作上下文。

        Args:
            symbol: 标的代码，如 000001.SZ。
            start_date: 增量区间开始日期（YYYY-MM-DD），None 表示全量。
            end_date: 增量区间截止日期（YYYY-MM-DD）。
            period: K线周期，默认 daily。
            adjust: 复权方式，默认 raw（不复权原始层）。
            records: 可选，调用方已拉取的增量区间原始记录（update_symbol 的 result["data"]）；
                传入则不重新拉取（消除增量双拉取），None 时按 start_date/end_date 自行拉取（向后兼容）。

        Returns:
            Optional[str]: 清洗合并后写出的 processed parquet 路径；
                无数据或清洗后为空返回 None。
        """
        import pandas as pd

        from myquant.data_access.cleaner.derived_metrics import (
            VOLUME_RATIO_WINDOW,
            compute_b_class_metrics,
            compute_derived_metrics,
        )
        from myquant.utils.file_utils import write_parquet

        # 与全量清洗同一份规则配置
        clean_config = load_clean_config()
        if not clean_config.get("enabled", True):
            logger.info("清洗功能已关闭，跳过增量清洗: %s", symbol)
            return None

        # M1修复：优先复用调用方已拉取记录（update_symbol result["data"]），
        # 消除增量链路 save 阶段二次网络请求；records 未传入（单调用/测试）才重新拉取，向后兼容。
        if records:
            interval_records: List[Dict[str, Any]] = list(records)
        else:
            # 拉取增量区间原始数据（绕过缓存，英文字段 date/open/...）
            data_result = self._fetcher.get_market_data(
                symbol, start_date=start_date, end_date=end_date,
                period=period, adjust=adjust, use_cache=False,
            )
            if not data_result or data_result.count == 0:
                return None
            interval_records = list(data_result.data)

        # 中英字段映射（fetcher/清洗管线用英文字段，扁平 parquet 用中文字段）
        _en_to_cn: Dict[str, str] = {
            "date": "日期", "open": "开盘价", "high": "最高价",
            "low": "最低价", "close": "收盘价", "volume": "成交量",
            "amount": "成交额", "pct_chg": "涨跌幅", "amplitude": "振幅",
            "volume_ratio": "量比", "float_share": "流通股本",
            "pe_dynamic": "市盈动", "turnover": "换手率",
            "eps": "每股收益",
        }
        _cn_to_en: Dict[str, str] = {v: k for k, v in _en_to_cn.items()}

        def _row_cn_to_en(row: "pd.Series") -> Dict[str, Any]:
            """将存量中文字段行转为清洗管线英文字段行。"""
            result: Dict[str, Any] = {}
            for cn, en in _cn_to_en.items():
                if cn in row.index:
                    val = row.get(cn)
                    if en == "date" and val is not None:
                        # parquet 日期为数值 YYYYMMDD（可能 float），先转 int 去掉 .0 再转字符串，
                        # 供清洗解析与后续上下文行剔除时精确匹配
                        try:
                            val = str(int(val))
                        except (ValueError, TypeError):
                            val = str(val)
                    result[en] = val
            return result

        def _row_en_to_cn(rec: Dict[str, Any]) -> Dict[str, Any]:
            """将清洗后英文字段行转为中文字段行。"""
            return {
                cn: rec.get(en)
                for en, cn in _en_to_cn.items() if en in rec
            }

        # 读存量扁平 parquet（一次读，既取前收盘上下文又用于最终合并）
        processed_dir = get_project_path("datasets", "processed")
        output_path = processed_dir / f"{symbol}.parquet"
        existing_df: Optional["pd.DataFrame"] = None
        if output_path.exists():
            try:
                existing_df = pd.read_parquet(output_path)
                # 归一化存量（兼容旧索引式/扁平 datetime64 → 整数「日期」列）
                existing_df = _normalize_processed_df(existing_df)
            except Exception:
                logger.warning("存量 processed parquet 读取失败，跳过合并: %s", output_path)

        # M3修复：前收盘上下文优先读 quant.db（增量写入权威），processed 兜底，
        # 避免 processed 落后导致跳变检测基准错；仅用于清洗，不写入。
        # M6扩展：上下文取前 N 根（VOLUME_RATIO_WINDOW=5），供量比 5 日均量基准，
        # 与全量清洗 compute_derived_metrics 口径一致（涨跌幅/振幅仅需前收盘，量比需前 5 日均量）。
        context_rows: List[Dict[str, Any]] = []
        if start_date:
            db_bars = self._read_db_prior_bars(
                symbol, start_date, period, adjust, limit=VOLUME_RATIO_WINDOW,
            )
            if db_bars:
                context_rows = db_bars
            elif existing_df is not None and not existing_df.empty:
                start_int = int(start_date.replace("-", ""))
                before = existing_df[existing_df["日期"].astype(int) < start_int]
                if not before.empty:
                    context_rows = [
                        _row_cn_to_en(r)
                        for _, r in before.tail(VOLUME_RATIO_WINDOW).iterrows()
                    ]

        # 组装清洗输入：前 N 根上下文 + 增量区间
        records_to_clean: List[Dict[str, Any]] = list(context_rows)
        records_to_clean.extend(interval_records)

        # 原始 bar 记录可能不含 symbol 字段，而清洗管线 check_symbol=true 默认校验；
        # 为同一标的的记录统一注入 symbol，避免整批误判为脏数据
        for rec in records_to_clean:
            rec.setdefault("symbol", symbol)

        # 执行清洗（同全量清洗规则）
        clean_result = run_clean_pipeline(
            records=records_to_clean,
            data_type="bar",
            symbol=symbol,
            config=clean_config,
            save_bad=False,
            save_report=False,
        )
        cleaned = clean_result["cleaned"]
        if not cleaned:
            # M5修复：清洗结果为空时必须告警（此前静默 return None，
            # 增量"拉取成功但 processed 未更新"无从观测）
            logger.warning(
                "增量清洗结果为空，跳过写 processed: %s (输入 %d 条)",
                symbol, len(records_to_clean),
            )
            return None

        # M6：与全量清洗同口径补派生量（A类：涨跌幅/振幅/量比；B类：换手率/流通股本/市盈动）。
        # 即使当下无消费者也计算写入，保证增量新行与全量重建的 processed 列口径长期一致。
        compute_derived_metrics(cleaned)
        if self._db:
            try:
                from ..database.queries import DataQueries
                dq = DataQueries(self._db)
                fin_rows = dq.conn.execute(
                    "SELECT report_date, circ_mv, total_mv, net_profit, eps "
                    "FROM financial_data WHERE symbol=? ORDER BY report_date",
                    (symbol,),
                )
                fin_all = [dict(r) for r in fin_rows]
                if fin_all:
                    fin_b = [
                        {
                            "report_date": f.get("report_date"),
                            "circ_mv": f.get("circ_mv"),
                            "total_mv": f.get("total_mv"),
                            "net_profit": f.get("net_profit"),
                        }
                        for f in fin_all
                    ]
                    # 归一字段：增量记录用 date（fetcher 原始格式），
                    # compute_b_class_metrics 内部按 trade_date 匹配财务（全量口径），
                    # 仅补齐 trade_date 供其使用，不影响 date 主字段。
                    for rec in cleaned:
                        if "date" in rec and "trade_date" not in rec:
                            rec["trade_date"] = rec["date"]
                    compute_b_class_metrics(cleaned, fin_b)
                    # 每股收益：按交易日匹配最近一期（同全量导出口径，防未来函数）
                    fin_dates = [str(f.get("report_date", "")) for f in fin_all]
                    fin_eps = [f.get("eps") for f in fin_all]
                    for rec in cleaned:
                        trade_date = str(rec.get("date") or "")
                        eps_val = None
                        for i, fd in enumerate(fin_dates):
                            if fd <= trade_date:
                                eps_val = fin_eps[i]
                            else:
                                break
                        rec["eps"] = eps_val
            except Exception as e:
                logger.warning("增量 B 类派生量计算失败，跳过（不影响写入）: %s: %s", symbol, e)

        # 剔除上下文行（已存在存量中，不重复写入）
        if context_rows:
            context_dates = {str(r.get("date")) for r in context_rows}
            cleaned = [r for r in cleaned if str(r.get("date")) not in context_dates]
            if not cleaned:
                # M5修复：剔除上下文行后无新增数据（本次增量区间无新 bar），
                # 不写 processed 属正常跳过，留痕便于审计
                logger.info("增量区间无新增数据，跳过写 processed: %s", symbol)
                return None

        # 清洗后英文字段 → 中文字段
        cleaned_cn = [_row_en_to_cn(r) for r in cleaned]
        cleaned_df = pd.DataFrame(cleaned_cn)
        # 日期统一为整数 YYYYMMDD，对齐存量 parquet 的 日期 列 dtype
        if "日期" in cleaned_df.columns:
            cleaned_df["日期"] = pd.to_numeric(
                cleaned_df["日期"].astype(str).str.replace("-", ""), errors="coerce"
            )

        # 与存量合并（硬约束：只追加缺失日期，冲突保留存量，绝不覆盖存量任何数据），
        # 列对齐存量 schema；写回前对存量部分做逐值等价校验，不等则抛异常不写出。
        merged = cleaned_df
        if existing_df is not None and not existing_df.empty:
            if not cleaned_df.empty:
                cleaned_df = cleaned_df.reindex(columns=existing_df.columns)
            if "日期" not in existing_df.columns or "日期" not in cleaned_df.columns:
                logger.warning("存量或增量缺少「日期」列，跳过写 processed: %s", symbol)
                return None
            existing_dates = set(int(d) for d in existing_df["日期"].dropna())
            new_dates = set(int(d) for d in cleaned_df["日期"].dropna())
            conflict = sorted(new_dates & existing_dates)
            if conflict:
                # 冲突行保留存量（keep=first 语义），剔除新行并告警，绝不覆盖
                logger.warning(
                    "增量区间与存量日期冲突 %d 条（保留存量，不覆盖）: %s 日期=%s",
                    len(conflict), symbol, conflict[:10],
                )
                cleaned_df = cleaned_df[~cleaned_df["日期"].astype(float).isin(
                    [float(d) for d in existing_dates]
                )]
                if cleaned_df.empty:
                    logger.info("增量行全部与存量冲突，无新增，跳过写 processed: %s", symbol)
                    return None
            merged = pd.concat([existing_df, cleaned_df], ignore_index=True)
            merged = merged.sort_values("日期").reset_index(drop=True)
            # 存量保护：合并结果中存量部分必须与读入时逐值等价（不等 → 抛异常，不写出）
            _verify_existing_unchanged(existing_df, merged, symbol)
        else:
            # 无存量新建：对齐全量 14 列模板（B 类列缺失置 None，列顺序与全量导出一致），
            # 修复增量新建文件列数=10（缺 换手率/流通股本/市盈动/每股收益）与全量 schema 不一致问题
            if not cleaned_df.empty:
                cleaned_df = cleaned_df.reindex(columns=PROCESSED_COLUMN_TEMPLATE)
                merged = cleaned_df

        write_parquet(output_path, merged, index=False)
        return str(output_path)


    def load_processed_data(
        self,
        symbol: str,
        data_type: str = "bar",
        period: str = "daily",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        从 datasets/processed/ 加载清洗后的数据

        【防未来函数】end_date 之后的数据不返回

        Parameters
        ----------
        symbol : str
            标的代码
        data_type : str
            数据类型: bar | tick
        period : str
            K线周期
        start_date : str | None
            过滤开始日期
        end_date : str | None
            过滤截止日期

        Returns
        -------
        List[Dict] : 清洗后的数据列表（按日期升序）
        """
        symbol_dir = get_project_path("datasets", "processed", symbol.replace(".", "_"))
        if not symbol_dir.exists():
            return []

        # 查找最新的 processed 文件
        pattern = f"{data_type}_*.csv"
        files = sorted(symbol_dir.glob(pattern), reverse=True)
        if not files:
            return []

        # 读取最新文件
        try:
            data = read_csv(str(files[0]))
        except Exception:
            return []

        # 按日期过滤
        if start_date or end_date:
            filtered = []
            for row in data:
                date_str = row.get("date", "")
                if start_date and date_str < start_date:
                    continue
                if end_date and date_str > end_date:
                    continue
                filtered.append(row)
            data = filtered

        return data

    # ═══════════════════════════════════════════
    # #O3修复：跨批次去重工具（基于date的upsert合并）
    # ═══════════════════════════════════════════

    @staticmethod
    def _deduplicate_records_by_date(
        new_records: List[Dict],
        old_records: List[Dict],
    ) -> List[Dict]:
        """
        基于 date 字段做跨批次去重合并（upsert 语义）。

        #O3修复：解决数据源反复拉取同一日期数据导致 processed 目录累积重复 bar 的问题。

        合并规则:
          - 同 date 时，新数据覆盖旧数据（upsert）
          - 旧数据中不在新数据中的日期保留
          - 新数据中不在旧数据中的日期追加
          - 最终按 date 升序排序返回

        【防未来函数】仅基于传入的已发生数据合并，不引入未来数据。

        Parameters
        ----------
        new_records : List[Dict]
            新批次数据（须含 date 字段）
        old_records : List[Dict]
            旧批次数据（须含 date 字段）

        Returns
        -------
        List[Dict] : 合并去重后的数据，按 date 升序
        """
        if not new_records:
            return list(old_records or [])
        if not old_records:
            return list(new_records)

        # 用字典做 upsert：key=date, value=record
        merged: Dict[str, Dict] = {}
        # 先放入旧数据
        for rec in old_records:
            date_key = str(rec.get("date", ""))
            if date_key:
                merged[date_key] = rec
        # 新数据覆盖旧数据（upsert）
        for rec in new_records:
            date_key = str(rec.get("date", ""))
            if date_key:
                merged[date_key] = rec

        # 按 date 升序排序
        result = sorted(merged.values(), key=lambda r: str(r.get("date", "")))
        return result

    def _find_latest_processed_file(
        self,
        symbol: str,
        data_type: str = "bar",
        period: str = "daily",
        adjust: Optional[str] = "raw",
    ) -> Optional[Path]:
        """
        查找同条件下最新的已保存 processed 文件。

        #O3修复：跨批次去重时用于读取上一批次数据做合并。

        文件命名规则: {data_type}_{period}_{adjust}_{timestamp}.csv
        按文件名中的时间戳排序，返回最新文件路径。

        Parameters
        ----------
        symbol : str
            标的代码
        data_type : str
            数据类型 bar/tick
        period : str
            K线周期
        adjust : str | None
            复权方式

        Returns
        -------
        Path | None : 最新文件路径，无匹配文件时返回 None
        """
        symbol_dir = get_project_path(
            "datasets", "processed", symbol.replace(".", "_")
        )
        if not symbol_dir.exists():
            return None

        adjust_suffix = f"_{adjust}" if adjust else ""
        prefix = f"{data_type}_{period}{adjust_suffix}_"

        matching_files = []
        for f in symbol_dir.iterdir():
            if f.is_file() and f.name.startswith(prefix) and f.name.endswith(".csv"):
                matching_files.append(f)

        if not matching_files:
            return None

        # 按文件名（含时间戳）排序，取最新
        matching_files.sort(key=lambda f: f.name, reverse=True)
        return matching_files[0]

    def _save_processed_data(
        self,
        records: List[Dict],
        symbol: str,
        data_type: str = "bar",
        period: str = "daily",
        adjust: Optional[str] = "raw",
        deduplicate_with_latest: bool = False,
    ) -> Optional[str]:
        """
        保存清洗后数据到 datasets/processed/

        #O3修复：增加 deduplicate_with_latest 参数，为 True 时与最新已保存文件
        基于 date 做 upsert 合并去重，解决数据源反复拉取同一日期数据导致
        processed 目录累积重复 bar 的问题。合并后保存为新文件（不覆盖旧文件，
        保持可追溯，不破坏版本管理机制）。

        Parameters
        ----------
        records : List[Dict]
            清洗后数据
        symbol : str
            标的代码
        data_type : str
            数据类型 bar/tick
        period : str
            K线周期
        adjust : str | None
            复权方式
        deduplicate_with_latest : bool
            #O3修复：是否与最新已保存文件合并去重（默认False，原行为不变）

        Returns
        -------
        str | None : 保存的文件路径
        """
        if not records:
            return None

        from datetime import datetime
        symbol_dir = ensure_dir(get_project_path(
            "datasets", "processed", symbol.replace(".", "_")
        ))

        # #O3修复：跨批次去重（与最新文件合并）
        saved_records = records
        dedup_stats = None
        if deduplicate_with_latest:
            latest_file = self._find_latest_processed_file(symbol, data_type, period, adjust)
            if latest_file and latest_file.exists():
                try:
                    old_records = read_csv(str(latest_file))
                    old_count = len(old_records)
                    new_count = len(records)
                    saved_records = self._deduplicate_records_by_date(records, old_records)
                    merged_count = len(saved_records)
                    dedup_stats = {
                        "old_count": old_count,
                        "new_count": new_count,
                        "merged_count": merged_count,
                        "duplicates_removed": old_count + new_count - merged_count,
                        "latest_file": str(latest_file),
                    }
                    logger.info(
                        f"[manager] 跨批次去重完成: symbol={symbol} "
                        f"旧={old_count} 新={new_count} 合并后={merged_count} "
                        f"去重={dedup_stats['duplicates_removed']}"
                    )
                except Exception as e:
                    logger.warning(f"[manager] 跨批次去重失败，使用原始数据保存: {e}")
                    saved_records = records

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        adjust_suffix = f"_{adjust}" if adjust else ""
        filename = f"{data_type}_{period}{adjust_suffix}_{timestamp}.csv"
        filepath = symbol_dir / filename

        try:
            write_csv(str(filepath), saved_records)
            # 记录去重统计到文件元数据（通过日志，不修改文件格式）
            if dedup_stats:
                logger.debug(f"[manager] 去重文件已保存: {filepath}, stats={dedup_stats}")
            return str(filepath)
        except Exception:
            return None

    # ═══════════════════════════════════════════
    # 数据集版本管理 (任务14：版本号/元数据/快照/回滚)
    # ═══════════════════════════════════════════

    def _versions_root(self, symbol: str) -> Path:
        """版本管理根目录 datasets/processed/{symbol}/versions"""
        symbol_dir = get_project_path("datasets", "processed", symbol.replace(".", "_"))
        return symbol_dir / "versions"

    def _version_dir(self, symbol: str, version: int) -> Path:
        """版本目录 datasets/processed/{symbol}/versions/v{version}"""
        return self._versions_root(symbol) / f"v{version}"

    def _read_version_index(self, symbol: str) -> Dict[str, Any]:
        """读取版本索引（不存在则返回空索引）"""
        index_path = self._versions_root(symbol) / "index.json"
        if not index_path.exists():
            return {
                "symbol": symbol,
                "updated_at": None,
                "head": {},
                "last_rollback": None,
                "versions": {},
            }
        try:
            index = read_json(index_path)
            index.setdefault("symbol", symbol)
            index.setdefault("head", {})
            index.setdefault("last_rollback", None)
            index.setdefault("versions", {})
            index.setdefault("updated_at", None)
            return index
        except Exception:
            # 索引损坏时重建空索引（不丢弃版本数据，仅丢失索引记录）
            return {
                "symbol": symbol,
                "updated_at": None,
                "head": {},
                "last_rollback": None,
                "versions": {},
            }

    def _write_version_index(self, symbol: str, index: Dict[str, Any]) -> None:
        """写版本索引（线程锁 + 跨进程文件锁保护）"""
        root = ensure_dir(self._versions_root(symbol))
        index_path = root / "index.json"
        index["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # 跨进程文件锁（容错：单进程环境锁失败不影响）
        try:
            with file_lock(str(root / ".index.lock"), timeout=5.0):
                write_json(str(index_path), index)
        except Exception:
            write_json(str(index_path), index)

    def _next_version(self, symbol: str, index: Dict[str, Any]) -> int:
        """计算下一个版本号（当前最大+1，空则1）"""
        existing = [int(v) for v in index.get("versions", {}).keys()]
        return (max(existing) + 1) if existing else 1

    @staticmethod
    def _extract_data_range(records: List[Dict]) -> Optional[Dict[str, str]]:
        """从记录提取数据时间范围（防未来函数：仅取当前记录中的日期）"""
        dates = [str(r["date"]) for r in records if r.get("date")]
        if not dates:
            return None
        return {"start_date": min(dates), "end_date": max(dates)}

    @staticmethod
    def _now_utc_str() -> str:
        """当前UTC时间字符串"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def create_dataset_version(
        self,
        symbol: str,
        records: List[Dict],
        data_type: str = "bar",
        period: str = "daily",
        adjust: Optional[str] = "raw",
        source: str = "offline",
        clean_run_id: Optional[str] = None,
        parent_version: Optional[int] = None,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DatasetVersion:
        """
        创建数据集新版本（版本号自动递增，生成快照 + 元数据 + 校验和）

        【防未来函数】仅基于传入的已发生数据创建版本，不引入未来数据。

        Parameters
        ----------
        symbol : str
            标的代码
        records : list[dict]
            数据集快照数据（清洗后 processed 数据，须含 date 字段）
        data_type : str
            数据类型 bar/tick
        period : str
            K线周期
        adjust : str | None
            复权方式
        source : str
            数据来源 offline/live
        clean_run_id : str | None
            关联任务13 clean_run_meta.run_id（清洗运行追溯）
        parent_version : int | None
            派生链上一版本（实盘增量版本）
        description : str
            版本说明
        metadata : dict | None
            扩展元数据

        Returns
        -------
        DatasetVersion : 新版本元数据
        """
        if not records:
            raise ValueError(f"创建版本失败：records 为空（symbol={symbol}）")
        with self._lock:
            index = self._read_version_index(symbol)
            version = self._next_version(symbol, index)

            # 写数据快照
            v_dir = ensure_dir(self._version_dir(symbol, version))
            meta = DatasetVersion(
                version=version,
                symbol=symbol,
                data_type=data_type,
                period=period,
                adjust=adjust,
                created_at=self._now_utc_str(),
                source=source,
                record_count=len(records),
                data_range=self._extract_data_range(records),
                clean_run_id=clean_run_id,
                parent_version=parent_version,
                description=description,
                metadata=metadata or {},
            )
            data_file = v_dir / meta.data_file_name
            write_csv(str(data_file), records)
            meta.checksum = file_sha256(str(data_file))

            # 写版本元数据
            write_json(str(v_dir / "metadata.json"), meta.to_dict())

            # 更新索引
            index["versions"][str(version)] = meta.to_dict()
            index["head"][data_type] = version
            self._write_version_index(symbol, index)
            return meta

    def create_dataset_version_from_clean_run(
        self,
        symbol: str,
        clean_run_id: str,
        records: Optional[List[Dict]] = None,
        data_type: str = "bar",
        period: str = "daily",
        adjust: Optional[str] = "raw",
        description: str = "",
        db_connection: Any = None,
    ) -> DatasetVersion:
        """
        从清洗运行记录创建数据集版本（任务13 clean_run_meta 无缝衔接）

        自动从 clean_run_meta 读取清洗元信息（total/valid/bad/valid_rate/
        quality_grade/source 等）补充到版本元数据，实现清洗→版本入库闭环。

        Parameters
        ----------
        symbol : str
            标的代码
        clean_run_id : str
            任务13 clean_run_meta.run_id（清洗运行ID）
        records : list[dict] | None
            数据集快照数据；None 时尝试从 processed_file 加载
        data_type : str
            数据类型 bar/tick
        period : str
            K线周期
        adjust : str | None
            复权方式
        description : str
            版本说明
        db_connection : DatabaseConnection | None
            数据库连接（读取 clean_run_meta），None 则用 self._db 或默认连接

        Returns
        -------
        DatasetVersion : 新版本元数据

        Raises
        ------
        ValueError : clean_run 不存在或无数据可创建版本
        """
        run_meta: Optional[Dict] = None
        # 读取清洗运行元信息
        try:
            conn = db_connection or self._db
            if conn is None:
                from myquant.database import get_connection
                conn = get_connection()
            from myquant.database import CleanDataQueries
            cq = CleanDataQueries(conn)
            run_meta = cq.get_clean_run(clean_run_id)
        except Exception:
            run_meta = None

        source = "offline"
        if run_meta:
            source = run_meta.get("source", "offline") or "offline"

        # 确定数据：优先显式传入，其次 processed_file
        if records is None:
            records = []
            if run_meta and run_meta.get("processed_file"):
                try:
                    records = read_csv(run_meta["processed_file"])
                except Exception:
                    records = []
            if not records:
                raise ValueError(
                    f"创建版本失败：clean_run={clean_run_id} 无数据可快照"
                    f"（需传入 records 或 processed_file 可读）"
                )

        # 构建扩展元数据（清洗统计）
        extra_meta: Dict[str, Any] = {"clean_run_meta": None}
        if run_meta:
            extra_meta["clean_run_meta"] = {
                "run_id": run_meta.get("run_id"),
                "status": run_meta.get("status"),
                "severity": run_meta.get("severity"),
                "total": run_meta.get("total"),
                "valid": run_meta.get("valid"),
                "bad": run_meta.get("bad"),
                "total_duration": run_meta.get("total_duration"),
            }

        return self.create_dataset_version(
            symbol=symbol,
            records=records,
            data_type=data_type,
            period=period,
            adjust=adjust,
            source=source,
            clean_run_id=clean_run_id,
            description=description,
            metadata=extra_meta,
        )

    def list_dataset_versions(
        self,
        symbol: str,
        data_type: Optional[str] = None,
    ) -> List[DatasetVersion]:
        """
        列出版本（按版本号降序）

        data_type 指定时仅返回该数据类型的版本。
        """
        with self._lock:
            index = self._read_version_index(symbol)
        versions = []
        for v_str, meta_dict in index.get("versions", {}).items():
            if data_type and meta_dict.get("data_type") != data_type:
                continue
            try:
                versions.append(DatasetVersion.from_dict(meta_dict))
            except Exception:
                continue
        versions.sort(key=lambda v: v.version, reverse=True)
        return versions

    def get_dataset_version(
        self,
        symbol: str,
        version: int,
        data_type: Optional[str] = None,
    ) -> Optional[DatasetVersion]:
        """查询指定版本元数据（不存在返回 None）"""
        with self._lock:
            index = self._read_version_index(symbol)
        meta_dict = index.get("versions", {}).get(str(version))
        if not meta_dict:
            return None
        if data_type and meta_dict.get("data_type") != data_type:
            return None
        return DatasetVersion.from_dict(meta_dict)

    def get_latest_dataset_version(
        self,
        symbol: str,
        data_type: str = "bar",
    ) -> Optional[DatasetVersion]:
        """获取指定数据类型的最新版本（head）"""
        with self._lock:
            index = self._read_version_index(symbol)
        head = index.get("head", {}).get(data_type)
        if head is None:
            return None
        meta_dict = index.get("versions", {}).get(str(head))
        return DatasetVersion.from_dict(meta_dict) if meta_dict else None

    def load_dataset_version(
        self,
        symbol: str,
        version: int,
        data_type: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        verify: bool = True,
    ) -> List[Dict]:
        """
        加载指定版本的数据快照

        【防未来函数】end_date 之后的数据绝对不返回。

        Parameters
        ----------
        symbol : str
            标的代码
        version : int
            版本号
        data_type : str | None
            数据类型（None 自动从版本元数据识别）
        start_date : str | None
            过滤开始日期
        end_date : str | None
            过滤截止日期（防未来函数关键参数）
        verify : bool
            是否校验 checksum（完整性）

        Returns
        -------
        list[dict] : 版本数据（按日期升序）

        Raises
        ------
        DatasetVersionNotFoundError : 版本不存在
        DataVersionIntegrityError : 完整性校验失败
        """
        with self._lock:
            index = self._read_version_index(symbol)
        meta_dict = index.get("versions", {}).get(str(version))
        if not meta_dict:
            raise DatasetVersionNotFoundError(
                f"版本不存在: symbol={symbol}, version={version}"
            )
        meta = DatasetVersion.from_dict(meta_dict)
        if data_type and meta.data_type != data_type:
            raise DatasetVersionNotFoundError(
                f"版本 {version} 数据类型为 {meta.data_type}，非 {data_type}"
            )

        data_file = self._version_dir(symbol, version) / meta.data_file_name
        if not data_file.exists():
            raise DatasetVersionNotFoundError(
                f"版本数据文件缺失: {data_file}"
            )

        # 完整性校验
        if verify and meta.checksum:
            actual = file_sha256(str(data_file))
            if actual != meta.checksum:
                raise DataVersionIntegrityError(
                    f"版本 {version} 数据完整性校验失败: {data_file}"
                )

        data = read_csv(str(data_file))

        # 按日期过滤（防未来函数：严格 end_date）
        if start_date or end_date:
            filtered = []
            for row in data:
                date_str = row.get("date", "")
                if start_date and date_str < start_date:
                    continue
                if end_date and date_str > end_date:
                    continue
                filtered.append(row)
            data = filtered

        # 按日期升序
        try:
            data.sort(key=lambda r: r.get("date", ""))
        except Exception as e:
            logger.debug(f"数据排序失败，保持原顺序: {e}")
        return data

    def rollback_dataset_version(
        self,
        symbol: str,
        version: int,
        data_type: str = "bar",
        period: str = "daily",
        adjust: Optional[str] = "raw",
        description: str = "",
    ) -> str:
        """
        回滚到指定版本

        将指定版本的数据快照写回 processed 当前目录（阶段2时间戳格式兼容，
        load_processed_data 的 glob 取最新文件机制会自动读到回滚数据），
        并在版本索引记录回滚事件（可追溯）。

        【防未来函数】回滚只使用已保存的历史版本快照，不引入未来数据。

        Parameters
        ----------
        symbol : str
            标的代码
        version : int
            目标版本号（回滚到此版本数据）
        data_type : str
            数据类型
        period : str
            K线周期
        adjust : str | None
            复权方式
        description : str
            回滚说明

        Returns
        -------
        str : 回滚后的当前数据文件路径

        Raises
        ------
        DatasetVersionNotFoundError : 版本不存在
        """
        data = self.load_dataset_version(symbol, version, data_type=data_type)
        if not data:
            raise DatasetVersionError(f"回滚失败：版本 {version} 数据为空")

        symbol_dir = ensure_dir(get_project_path(
            "datasets", "processed", symbol.replace(".", "_")
        ))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        adjust_suffix = f"_{adjust}" if adjust else ""
        filename = f"{data_type}_{period}{adjust_suffix}_{timestamp}_rollback_v{version}.csv"
        filepath = symbol_dir / filename
        write_csv(str(filepath), data)

        # 记录回滚事件到版本索引
        with self._lock:
            index = self._read_version_index(symbol)
            index["last_rollback"] = {
                "to_version": version,
                "data_type": data_type,
                "rolled_back_at": self._now_utc_str(),
                "file": str(filepath),
                "description": description,
            }
            self._write_version_index(symbol, index)

        return str(filepath)

    def delete_dataset_version(
        self,
        symbol: str,
        version: int,
        data_type: Optional[str] = None,
    ) -> bool:
        """
        删除指定版本（移除快照目录 + 更新索引与 head）

        Returns
        -------
        bool : 是否删除成功（版本不存在返回 False）
        """
        with self._lock:
            index = self._read_version_index(symbol)
            meta_dict = index.get("versions", {}).get(str(version))
            if not meta_dict:
                return False
            if data_type and meta_dict.get("data_type") != data_type:
                return False

            # 删除版本目录
            v_dir = self._version_dir(symbol, version)
            import shutil
            shutil.rmtree(v_dir, ignore_errors=True)

            # 更新索引
            index["versions"].pop(str(version), None)
            # 修正 head：若删除的是 head，回退到该类型前一个版本
            dt = meta_dict.get("data_type", "bar")
            if index.get("head", {}).get(dt) == version:
                remaining = [
                    int(v) for v, m in index["versions"].items()
                    if m.get("data_type") == dt
                ]
                index["head"][dt] = max(remaining) if remaining else None
                if index["head"].get(dt) is None:
                    index["head"].pop(dt, None)
            self._write_version_index(symbol, index)
            return True

    def verify_dataset_version(
        self,
        symbol: str,
        version: int,
        data_type: Optional[str] = None,
    ) -> bool:
        """校验版本数据完整性（checksum）"""
        with self._lock:
            index = self._read_version_index(symbol)
        meta_dict = index.get("versions", {}).get(str(version))
        if not meta_dict:
            return False
        meta = DatasetVersion.from_dict(meta_dict)
        data_file = self._version_dir(symbol, version) / meta.data_file_name
        if not data_file.exists():
            return False
        if not meta.checksum:
            return data_file.exists()
        return file_sha256(str(data_file)) == meta.checksum

    def get_dataset_versions_metadata(
        self,
        symbol: str,
        data_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        数据集版本汇总信息

        Returns
        -------
        dict : {
            symbol, version_count, head, last_rollback,
            versions: [DatasetVersion.to_dict(), ...],  # 按版本号降序
        }
        """
        with self._lock:
            index = self._read_version_index(symbol)
        versions = self.list_dataset_versions(symbol, data_type=data_type)
        return {
            "symbol": symbol,
            "version_count": len(versions),
            "head": index.get("head", {}),
            "last_rollback": index.get("last_rollback"),
            "versions": [v.to_dict() for v in versions],
        }

    def __repr__(self) -> str:
        report = self.get_sync_report()
        return (
            f"DataManager(symbols={report['total_symbols']}, "
            f"completed={report['completed']}, failed={report['failed']})"
        )
