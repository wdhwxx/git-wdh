# 文件名：myquant/data_access/admission.py
"""
门禁①：ODS 入库前准入（数据层施工 B-04 #444）
====================================================
依据《数据层结构优化可行性施工方案报告_v1.1》§4.3(d)(e)：
  - BLOCK：必需字段缺失 / 数值类型不可转 / high<low / 负价 / 空 symbol / 时间戳无效
    → 拒收（C-01 接线后写入 admission_reject 表留痕）
  - FLAG：价格跳变（分品种阈值）/ 量异动 / 缺失行 / 源切换
    → 标记写 anomaly_flags 列并随行入库（B-02 已落列）
  - PASS：通过
  - 白名单：admission_whitelist.yaml（生效窗 + 条目），命中直接放行
  - 阈值配置化分品种：data_clean.yaml bar.max_price_jump_pct
    （default=0.22 兼容现有读取 + by_market + by_symbol 二级覆盖）
新模块默认不接线（C-01 接入 update_symbol 才生效；B-05 影子模式先行比对）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# 必需价格字段（任一缺失/不可转数值即 BLOCK）
_REQUIRED_PRICE_FIELDS = ("open", "high", "low", "close")
# 可空但参与 FLAG 判定的字段
_VOLUME_FIELD = "volume"

# 交易日格式（YYYY-MM-DD）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 板别判定（按 symbol 前缀，覆盖分品种阈值键）
_BOARD_RULES = (
    ("科创板", lambda s: s.startswith("688")),
    ("创业板", lambda s: s.startswith("30")),
    ("北交所", lambda s: s.startswith(("8", "4"))),
    ("主板", lambda s: True),
)

# 默认分品种跳变阈值（data_clean.yaml 未配置时兜底；主板 10% 涨停+容差、创业/科创 20%+容差、北交所 30%+容差）
_DEFAULT_BY_MARKET_THRESHOLDS = {
    "主板": 0.11,
    "创业板": 0.21,
    "科创板": 0.21,
    "北交所": 0.31,
}
_DEFAULT_MAX_PRICE_JUMP_PCT = 0.22  # data_clean.yaml 原默认（兼容）
_DEFAULT_MAX_VOLUME_RATIO = 100.0


@dataclass
class AdmissionResult:
    """准入判定结果。

    Attributes:
        decision: BLOCK / FLAG / PASS 三态
        reasons: BLOCK/FLAG 触发的规则描述列表
        flags: 应写入 anomaly_flags 列的标记列表（FLAG 态）
    """

    decision: str = "PASS"
    reasons: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """是否放行（PASS 或 FLAG 均放行，BLOCK 拒收）。"""
        return self.decision != "BLOCK"


class AdmissionChecker:
    """门禁①ODS 准入判定器（无状态，可并发）。"""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        whitelist_path: Optional[Path] = None,
    ) -> None:
        """初始化准入判定器。

        Args:
            config_path: data_clean.yaml 路径（默认 config/data_clean.yaml）
            whitelist_path: admission_whitelist.yaml 路径（默认 config/admission_whitelist.yaml）
        """
        self._config_path = config_path or self._default_path("data_clean.yaml")
        self._whitelist_path = whitelist_path or self._default_path("admission_whitelist.yaml")
        self._max_volume_ratio = _DEFAULT_MAX_VOLUME_RATIO
        self._load_config()

    @staticmethod
    def _default_path(name: str) -> Path:
        """定位 config 目录（项目根 config/，兼容当前工作目录）。"""
        cand = [
            Path.cwd() / "config" / name,
            Path(__file__).resolve().parents[2] / "config" / name,
        ]
        for p in cand:
            if p.exists():
                return p
        return cand[0]

    def _load_config(self) -> None:
        """加载 data_clean.yaml（跳变阈值/量异动；缺文件用默认值不报错）。"""
        try:
            if not self._config_path.exists():
                logger.warning("准入配置缺失 %s，使用默认阈值", self._config_path)
                return
            raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
            bar = raw.get("bar", {}) or {}
            self._max_volume_ratio = float(
                bar.get("max_volume_ratio", _DEFAULT_MAX_VOLUME_RATIO)
            )
        except Exception as exc:  # 配置损坏不阻断准入（降级默认，记录日志）
            logger.warning("准入配置解析失败 %s: %s，使用默认阈值", self._config_path, exc)

    # ── 阈值解析 ─────────────────────────────────────────
    def _resolve_price_jump_threshold(self, symbol: str) -> float:
        """分品种跳变阈值：by_symbol > by_market > default。"""
        try:
            if self._config_path.exists():
                raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
                bar = raw.get("bar", {}) or {}
                by_symbol = bar.get("max_price_jump_pct_by_symbol") or {}
                if symbol in by_symbol:
                    return float(by_symbol[symbol])
                by_market = bar.get("max_price_jump_pct_by_market") or {}
                board = self._board_of(symbol)
                if board in by_market:
                    return float(by_market[board])
                return float(bar.get("max_price_jump_pct", _DEFAULT_MAX_PRICE_JUMP_PCT))
        except Exception as exc:  # 配置异常降级默认
            logger.warning("分品种阈值解析失败 %s: %s", symbol, exc)
        return _DEFAULT_MAX_PRICE_JUMP_PCT

    @staticmethod
    def _board_of(symbol: str) -> str:
        """按 symbol 前缀判定板别。"""
        for board, match in _BOARD_RULES:
            if match(symbol):
                return board
        return "主板"

    # ── 白名单 ─────────────────────────────────────────
    def _whitelisted(self, symbol: str, rule: str) -> bool:
        """生效窗内白名单命中即 True（enabled=false 时不生效）。"""
        try:
            if not self._whitelist_path.exists():
                return False
            raw = yaml.safe_load(self._whitelist_path.read_text(encoding="utf-8")) or {}
            if not raw.get("enabled", False):
                return False
            today = date.today().isoformat()
            for entry in raw.get("entries", []) or []:
                if entry.get("symbol") != symbol or entry.get("rule") != rule:
                    continue
                valid_from = entry.get("valid_from")
                valid_until = entry.get("valid_until")
                if valid_from and today < valid_from:
                    continue
                if valid_until and today > valid_until:
                    continue
                return True
        except Exception as exc:  # 白名单异常不放大（fail-open 仅白名单判定，BLOCK 规则仍生效）
            logger.warning("白名单解析失败: %s", exc)
        return False

    # ── 判定主入口 ─────────────────────────────────────────
    def check(
        self,
        row: Dict[str, Any],
        symbol: str,
        data_source: str,
        prev_row: Optional[Dict[str, Any]] = None,
        rule: str = "ohlc",
    ) -> AdmissionResult:
        """对单行做准入判定（BLOCK/FLAG/PASS）。

        Args:
            row: 行情行（open/high/low/close/volume/trade_date 等字段）
            symbol: 标的代码
            data_source: 数据源标识
            prev_row: 前一根行情（价格跳变/量异动/缺失行/源切换判定上下文）
            rule: 触发规则名（白名单匹配用，默认 ohlc）

        Returns:
            AdmissionResult：PASS / FLAG（放行+标记）/ BLOCK（拒收）
        """
        if self._whitelisted(symbol, rule):
            return AdmissionResult(decision="PASS", reasons=["whitelisted"])

        block_reasons: List[str] = []
        # ── BLOCK：空 symbol ──
        if not symbol:
            block_reasons.append("empty_symbol")

        # ── BLOCK：时间戳无效 ──
        trade_date = str(row.get("trade_date", "") or "")
        if not _DATE_RE.match(trade_date):
            block_reasons.append(f"invalid_timestamp:{trade_date!r}")

        # ── BLOCK：必需字段缺失 / 类型不可转 / 负价 / high<low ──
        prices: Dict[str, float] = {}
        for fld in _REQUIRED_PRICE_FIELDS:
            val = row.get(fld)
            if val is None or (isinstance(val, float) and val != val):  # None/NaN
                block_reasons.append(f"missing_field:{fld}")
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                block_reasons.append(f"unconvertible:{fld}={val!r}")
                continue
            if fval < 0:
                block_reasons.append(f"negative_price:{fld}={fval}")
            prices[fld] = fval

        if len(prices) == 4 and prices["high"] < prices["low"]:
            block_reasons.append(f"high_low_conflict:high={prices['high']} low={prices['low']}")

        if block_reasons:
            return AdmissionResult(decision="BLOCK", reasons=block_reasons)

        # ── FLAG：BLOCK 未触发，继续规则标记 ──
        flags: List[str] = []
        if prev_row is not None:
            # 价格跳变（分品种阈值）
            prev_close = prev_row.get("close")
            close = prices["close"]
            if prev_close not in (None, 0, ""):
                try:
                    jump = abs(close / float(prev_close) - 1.0)
                    threshold = self._resolve_price_jump_threshold(symbol)
                    if jump > threshold:
                        flags.append(
                            f"price_jump:{jump:.4f}>threshold:{threshold:.4f}"
                        )
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            # 量异动
            cur_vol = row.get(_VOLUME_FIELD)
            prev_vol = prev_row.get(_VOLUME_FIELD)
            if cur_vol not in (None, 0, "") and prev_vol not in (None, 0, ""):
                try:
                    ratio = float(cur_vol) / float(prev_vol)
                    if ratio > self._max_volume_ratio:
                        flags.append(f"volume_anomaly:ratio={ratio:.1f}")
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            # 缺失行（相邻记录间隔过大，简化：>3 自然日）
            if trade_date and prev_row.get("trade_date"):
                try:
                    gap = (
                        datetime.strptime(trade_date, "%Y-%m-%d")
                        - datetime.strptime(str(prev_row["trade_date"]), "%Y-%m-%d")
                    ).days
                    if gap > 3:
                        flags.append(f"missing_rows:gap_days={gap}")
                except ValueError:
                    pass
            # 源切换
            if prev_row.get("data_source") not in (None, data_source):
                flags.append(
                    f"source_switch:{prev_row.get('data_source')}->{data_source}"
                )

        if flags:
            return AdmissionResult(decision="FLAG", flags=flags)
        return AdmissionResult(decision="PASS")
