# 文件名：myquant/data_access/writer_shadow.py
"""
统一写入口影子模式（数据层施工 B-05 #445）
============================================
依据《数据层结构优化可行性施工方案报告_v1.1》§5.2 B-05 / §4.3(a)：
  现网写路径（manager._save_to_database → bulk_insert_market_data）逐行入库；
  影子路径对同一批原始行过门禁①（admission.py）：
    - BLOCK → 拒收清单（不产生写入行，C-01 接线后落 admission_reject 留痕）
    - FLAG  → anomaly_flags 落列（修复 K4：_warnings 被丢弃的失败点）
    - PASS  → 原样放行
  再补 ingest_time / ingest_batch 采集元数据列（B-02 #442 已落列）。
  compare()：除 ingest 三列外逐字段比对现网与影子 → ShadowReport。
影子开关 enabled 默认 False（生产零影响，C-03 收敛写入口后才接线）；
生产验证：连续 3 交易日差异 = 0（V-05）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from myquant.data_access.admission import AdmissionChecker, AdmissionResult

logger = logging.getLogger(__name__)

# 影子比对豁免列：采集元数据/标记列（影子侧有值、现网侧 None 属预期差异）
_SHADOW_EXEMPT_COLUMNS = {"ingest_time", "ingest_batch", "anomaly_flags"}


@dataclass
class ShadowReport:
    """影子比对报告。

    Attributes:
        checked_rows: 参与比对的原始行数
        consistent: 豁免列外逐字段一致（差异=0）
        diffs: 不一致明细列表（[行号, 字段, 现网值, 影子值]）
        blocked: BLOCK 拒收行摘要（[行号, 原因]）
        flagged: FLAG 打标行摘要（[行号, flags]）
    """

    checked_rows: int = 0
    consistent: bool = True
    diffs: List[List[Any]] = field(default_factory=list)
    blocked: List[List[Any]] = field(default_factory=list)
    flagged: List[List[Any]] = field(default_factory=list)


class WriteShadowComparer:
    """现网写路径 vs 门禁①影子写路径的逐字段比对器（无状态）。"""

    def __init__(
        self,
        checker: Optional[AdmissionChecker] = None,
        enabled: bool = False,
    ) -> None:
        """初始化影子比对器。

        Args:
            checker: 门禁①判定器（默认 AdmissionChecker()，读真实配置）
            enabled: 影子开关（默认 False，生产零影响）
        """
        self._checker = checker or AdmissionChecker()
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── 影子写路径 ─────────────────────────────────────────
    def shadow_rows(
        self,
        raw_rows: List[Dict[str, Any]],
        symbol: str,
        market: str,
        period: str,
        adjust: Optional[str],
        data_source: str,
    ) -> tuple[List[Dict[str, Any]], List[List[Any]], List[List[Any]]]:
        """对原始行批量过门禁①，产出影子写入行 + BLOCK/FLAG 摘要。

        Args:
            raw_rows: 原始行情行（与现网 _save_to_database 输入同源）
            symbol/market/period/adjust: 入库维度字段
            data_source: 数据源标识

        Returns:
            (shadow_db_rows, blocked_summary, flagged_summary)：
            shadow_db_rows 仅含 PASS/FLAG 行（BLOCK 拒收不产生写入行）
        """
        shadow_db_rows: List[Dict[str, Any]] = []
        blocked: List[List[Any]] = []
        flagged: List[List[Any]] = []
        ingest_time = datetime.now().isoformat(timespec="seconds")
        seq = 0
        prev_row: Optional[Dict[str, Any]] = None
        for idx, row in enumerate(raw_rows):
            # 原始行情行用 date 键，门禁①读 trade_date —— 准入视图映射
            admission_row = dict(row)
            admission_row.setdefault("trade_date", row.get("date", ""))
            result: AdmissionResult = self._checker.check(
                admission_row, symbol, data_source, prev_row=prev_row
            )
            seq += 1
            if result.decision == "BLOCK":
                blocked.append([idx, "|".join(result.reasons)])
                continue  # 影子拒收：不产生写入行
            flags_str = ",".join(result.flags) if result.flags else None
            if result.decision == "FLAG":
                flagged.append([idx, flags_str])
            shadow_db_rows.append(
                {
                    "symbol": symbol,
                    "market": market,
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
                    "data_source": data_source,
                    "ingest_time": ingest_time,
                    "ingest_batch": f"{datetime.now():%Y%m%d}-{seq:04d}",
                    "anomaly_flags": flags_str,
                }
            )
            prev_row = admission_row
        return shadow_db_rows, blocked, flagged

    # ── 比对 ─────────────────────────────────────────
    def compare(
        self,
        legacy_db_rows: List[Dict[str, Any]],
        shadow_db_rows: List[Dict[str, Any]],
    ) -> ShadowReport:
        """逐字段比对现网与影子写入行（豁免 ingest 三列）。

        Args:
            legacy_db_rows: 现网写路径产出的 db_data
            shadow_db_rows: 影子写路径产出（shadow_rows 返回值）

        Returns:
            ShadowReport：consistent=True 表示豁免列外零差异
        """
        report = ShadowReport(checked_rows=len(shadow_db_rows))
        if not self._enabled:
            # 影子关闭：不产生比对结论（生产零影响）
            report.consistent = True
            return report
        for idx, (legacy, shadow) in enumerate(
            zip(legacy_db_rows, shadow_db_rows)
        ):
            for key in legacy:
                if key in _SHADOW_EXEMPT_COLUMNS:
                    continue
                if key not in shadow:
                    report.diffs.append([idx, key, legacy.get(key), "<missing>"])
                    continue
                if legacy.get(key) != shadow.get(key):
                    report.diffs.append(
                        [idx, key, legacy.get(key), shadow.get(key)]
                    )
        report.consistent = not report.diffs
        return report

    # ── 一站式：输入原始行直接产出比对报告 ──────────────────
    def run_compare(
        self,
        legacy_db_rows: List[Dict[str, Any]],
        raw_rows: List[Dict[str, Any]],
        symbol: str,
        market: str,
        period: str,
        adjust: Optional[str],
        data_source: str,
    ) -> ShadowReport:
        """影子一站式入口：现网行 + 原始行 → 影子写 → 比对报告。

        legacy_db_rows 应来自现网写路径（manager._save_to_database 产物）。
        """
        shadow_rows, blocked, flagged = self.shadow_rows(
            raw_rows, symbol, market, period, adjust, data_source
        )
        report = self.compare(legacy_db_rows, shadow_rows)
        report.blocked = blocked
        report.flagged = flagged
        return report
