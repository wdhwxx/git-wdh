# 文件名：myquant/backtest/param_optimizer.py
"""
策略参数优化器（网格搜索 + 并行加速）
====================================
对策略参数网格做穷举评估，接入 P2 并行治理层：
  - ParallelManager 计算进程池并行评估参数组合（多标的 x 多参数组）
  - parallel_factor_context 上下文：进入自动关闭 factor_cache L1 缓存并告警，
    退出自动恢复（支持嵌套），杜绝并行期间 L1 命中率低下的隐性损耗
  - pickle 预检：任务提交进程池前自动拦截不可 pickle 任务（pickle_checker）
  - StageTimer 分阶段计时（由入口脚本接入，定位各阶段耗时）

核心组件:
  - ParamSpec / ParamGrid : 参数网格定义（离散候选值，笛卡尔积展开）
  - ParamTrial            : 单次评估任务（可 pickle，供子进程反序列化）
  - evaluate_params       : 模块级任务函数（进程池可 pickle 的顶层函数）
  - ParamOptimizer        : 优化器（并行调度、指标排序、最优参数输出）
  - ParamOptimizationResult : 聚合结果（summary/to_dict/to_dataframe）

设计原则:
  - Windows spawn 约束：任务函数与策略工厂必须位于本真实模块（可被子进程 import），
    执行入口放 scripts/ 的 if __name__ == "__main__" 下
  - 任务函数永不抛出：子进程内 try/except 捕获，异常记入 status="error"，
    配合 return_exceptions 双重兜底，单组失败不影响整批
  - 并行上下文只包裹评估段，数据准备/排序输出在单进程完成，避免无谓串行
"""

from __future__ import annotations

import itertools
from functools import partial
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from myquant.backtest.data_loader import require_ohlcv
from myquant.backtest.engine import BacktestScheduler, register_broker_factory
from myquant.models.backtest import BacktestConfig
from myquant.strategies.base import StrategyBase
from myquant.strategies.ma_strategy import MACrossStrategy
from myquant.utils.parallel import parallel_factor_context
from myquant.utils.timer import format_duration
from myquant.realism_gate.signature_guard import assert_uniform_signature

logger = logging.getLogger("myquant.backtest.param_optimizer")

# ── 常量（禁魔法数字） ──────────────────────────────────────────────
DEFAULT_OBJECTIVE: str = "sharpe_ratio"
DEFAULT_INITIAL_CAPITAL: float = 1_000_000.0
DEFAULT_FREQUENCY: str = "1d"
RANK_TOP_N: int = 10

# 目标指标白名单：指标名 -> 排序方向（True=越大越好，False=越小越好）
# 注：max_drawdown 为负值，越小（越接近 0）越好，故方向为 False
OBJECTIVE_DIRECTION: Dict[str, bool] = {
    "sharpe_ratio": True,
    "total_return": True,
    "max_drawdown": False,
}

# 策略注册表：策略名 -> 策略类（扩展新策略只需在此注册）
STRATEGY_REGISTRY: Dict[str, type] = {
    "ma_cross": MACrossStrategy,
}


# ── 参数网格定义 ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class ParamSpec:
    """单个参数的候选值集合（离散、去重保序）。

    Attributes:
        name: 参数名（须与策略构造参数一致）。
        values: 候选值元组（不可变、非空、去重保序）。
    """

    name: str
    values: Tuple[Any, ...]

    def __post_init__(self) -> None:
        """防御式校验：参数名非空字符串，候选值非空。"""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(f"参数名必须为非空字符串，收到: {self.name!r}")
        if not isinstance(self.values, (tuple, list)) or len(self.values) == 0:
            raise ValueError(f"参数 {self.name} 的候选值集合不能为空")
        # 冻结为元组并去重保序
        seen: List[Any] = []
        for v in self.values:
            if v not in seen:
                seen.append(v)
        object.__setattr__(self, "values", tuple(seen))

    def __len__(self) -> int:
        """候选值数量。"""
        return len(self.values)


@dataclass(frozen=True)
class ParamGrid:
    """参数网格：多个 ParamSpec 的笛卡尔积组合。

    Attributes:
        specs: 参数规格元组（至少一个）。
    """

    specs: Tuple[ParamSpec, ...]

    def __post_init__(self) -> None:
        """防御式校验：至少一个参数规格。"""
        if not isinstance(self.specs, (tuple, list)) or len(self.specs) == 0:
            raise ValueError("参数网格至少需要一个参数规格")
        names = [s.name for s in self.specs]
        if len(names) != len(set(names)):
            raise ValueError(f"参数网格存在重复参数名: {names}")

    def __len__(self) -> int:
        """组合总数（笛卡尔积）。"""
        total: int = 1
        for spec in self.specs:
            total *= len(spec)
        return total

    def combinations(self) -> List[Dict[str, Any]]:
        """展开为全部参数组合字典列表（笛卡尔积，按规格顺序）。"""
        value_lists: List[List[Any]] = [list(spec.values) for spec in self.specs]
        result: List[Dict[str, Any]] = []
        for combo in itertools.product(*value_lists):
            result.append(dict(zip([s.name for s in self.specs], combo)))
        return result


# ── 评估任务与任务函数（可 pickle） ───────────────────────────────────
@dataclass(frozen=True)
class ParamTrial:
    """单次参数评估任务（可被子进程反序列化）。

    Attributes:
        params: 本组参数组合字典。
        strategy_name: 策略注册名（见 STRATEGY_REGISTRY）。
        symbol: 标的代码。
        data: 单标的行情字典 {symbol: DataFrame}（只含本标的，减小序列化体积）。
        start_date: 回测起始日期（YYYY-MM-DD）。
        end_date: 回测结束日期（YYYY-MM-DD）。
        initial_capital: 初始资金。
        frequency: K 线频率。
        split: 区段标识：'in'=样本内（寻优）、'out'=样本外（确认）；
            无 in_sample_end 分割时恒为 'in'（KP-P2-05）。
    """

    params: Dict[str, Any]
    strategy_name: str
    symbol: str
    data: Dict[str, pd.DataFrame]
    start_date: str
    end_date: str
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    frequency: str = DEFAULT_FREQUENCY
    split: str = "in"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可读字典（data 仅记标的名，避免整块数据入报告）。"""
        return {
            "params": self.params,
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "frequency": self.frequency,
            "split": self.split,
        }


def create_strategy(name: str, params: Dict[str, Any]) -> StrategyBase:
    """按注册名创建策略实例（模块级工厂，供子进程 pickle 引用）。

    Args:
        name: 策略注册名。
        params: 策略构造参数（含默认值合并）。

    Returns:
        策略实例。

    Raises:
        ValueError: 策略名未注册。
        TypeError: 策略构造参数非法。
    """
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"未知策略: {name!r}，已注册策略: {sorted(STRATEGY_REGISTRY)}"
        )
    return cls(**params)


def evaluate_params(
    trial: ParamTrial,
    broker_factory: Optional[Callable[[Any], Any]] = None,
) -> Dict[str, Any]:
    """模块级任务函数：在子进程中评估一组参数，返回指标字典（永不抛出）。

    任何异常都被捕获并记入 status="error"（含错误类型与消息），
    保证进程池 map_cpu 不因单组失败中断整批评估。

    Args:
        trial: 评估任务。

    Returns:
        指标字典：
            - params: 参数组合
            - symbol: 标的
            - status: "completed" / "error"
            - total_return / sharpe_ratio / max_drawdown / trade_count: 绩效
            - error: 错误信息（status="error" 时非空）
    """
    base: Dict[str, Any] = {
        "params": trial.params,
        "symbol": trial.symbol,
        "split": trial.split,
        "status": "error",
        "total_return": None,
        "sharpe_ratio": None,
        "max_drawdown": None,
        "trade_count": None,
        "error": None,
    }
    try:
        strategy = create_strategy(trial.strategy_name, trial.params)
        config = BacktestConfig(
            strategy_name=trial.strategy_name,
            symbols=[trial.symbol],
            start_date=trial.start_date,
            end_date=trial.end_date,
            frequency=trial.frequency,
            initial_capital=trial.initial_capital,
            params=trial.params,
        )
        # P1-04：走 create_engine（旁路收敛，门禁软接线统一入口）
        scheduler = BacktestScheduler(mode="auto")
        engine = scheduler.create_engine(
            config=config, strategy=strategy, data=trial.data,
        )
        result = engine.run()
        metrics = result.metrics
        base.update(
            {
                "status": "completed",
                "total_return": float(metrics.total_return),
                "sharpe_ratio": float(metrics.sharpe_ratio),
                "max_drawdown": float(metrics.max_drawdown),
                "trade_count": int(len(result.trades)),
            }
        )
        # P1-16（#366）：记录本轮回测的 manifest 签名，供聚合排行按签名分组、
        # 跨签名拒绝合并（§4.10）。门禁软接线（_realism_manifest=None）时留 None。
        try:
            _manifest = getattr(engine, "_realism_manifest", None)
            base["manifest_signature"] = (
                _manifest.manifest_signature() if _manifest is not None else None
            )
        except Exception as exc:  # noqa: BLE001 - 签名采集失败不阻断回测主流程
            base["manifest_signature"] = None
            logger.warning(f"manifest 签名采集失败（留 None 不阻断）: {exc}")
    except Exception as exc:  # noqa: BLE001 - 任务函数必须隔离所有异常
        base["error"] = f"{type(exc).__name__}: {exc}"
    return base


# ── 区段合并（KP-P2-05 样本内/样本外双列） ────────────────────────────

# 结论指标字段名（与 evaluate_params 输出一致）
_CONCLUSION_METRICS: Tuple[str, ...] = (
    "sharpe_ratio",
    "total_return",
    "max_drawdown",
    "trade_count",
)


def _coalesce_outcomes(
    normalized: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """把按 (params, symbol, split) 展开的评估结果合并为按 (params, symbol) 的聚合记录。

    每条聚合记录携带双区段指标（in_*/out_*）与结论字段：
      - in_*：样本内指标（寻优依据）；
      - out_*：样本外指标（确认依据）；
      - 结论字段（sharpe_ratio 等）：有样本外且完成时取样本外，否则回退样本内——
        即"结论以样本外为准"（KP-P2-05），无分割时等价于原全样本指标。

    Args:
        normalized: 归一化后的逐区段评估结果列表（含 split 字段）。

    Returns:
        按参数组合聚合的结果字典列表，status 聚合为 "completed"/"error"。
    """
    groups: Dict[Tuple[str, Any], Dict[str, Optional[Dict[str, Any]]]] = {}
    order: List[Tuple[str, Any]] = []
    for outcome in normalized:
        # params 为 dict，不可哈希，用其稳定 repr 作分组键
        key = (repr(outcome.get("params")), outcome.get("symbol"))
        if key not in groups:
            groups[key] = {"in": None, "out": None}
            order.append(key)
        seg = outcome.get("split", "in")
        groups[key][seg] = outcome

    combined: List[Dict[str, Any]] = []
    for key in order:
        segs = groups[key]
        in_o = segs.get("in")
        out_o = segs.get("out")
        # has_out 区分分割模式（有 out 区段）与非分割模式（仅 in，out 段不存在）
        has_out = out_o is not None
        in_o = in_o or {}
        out_o = out_o or {}
        params = in_o.get("params")
        if params is None:
            params = out_o.get("params")
        record: Dict[str, Any] = {"params": params, "symbol": key[1]}
        # 双区段指标列
        for metric in _CONCLUSION_METRICS:
            record[f"in_{metric}"] = in_o.get(metric)
            record[f"out_{metric}"] = out_o.get(metric)
        # 结论字段：样本外完成优先，否则回退样本内
        in_ok = bool(in_o.get("status") == "completed")
        out_ok = bool(out_o.get("status") == "completed")
        source = out_o if out_ok else (in_o if in_ok else {})
        for metric in _CONCLUSION_METRICS:
            record[metric] = source.get(metric)
        # 聚合状态与错误（非分割模式：仅需 in 完成即视为成功）
        if in_ok and (not has_out or out_ok):
            record["status"] = "completed"
            record["error"] = None
        elif in_ok:  # 分割模式：样本内成功、样本外失败
            record["status"] = "error"
            record["error"] = out_o.get("error") or "样本外评估失败"
        elif not has_out:  # 非分割模式：样本内失败
            record["status"] = "error"
            record["error"] = in_o.get("error") or "评估失败"
        elif out_ok:  # 分割模式：样本内失败、样本外成功（异常情形，仍以样本外为结论）
            record["status"] = "error"
            record["error"] = in_o.get("error") or "样本内评估失败"
        else:
            record["status"] = "error"
            record["error"] = (in_o.get("error") or out_o.get("error") or "评估失败")
        combined.append(record)
    return combined


# ── 优化结果容器 ─────────────────────────────────────────────────────
@dataclass
class ParamOptimizationResult:
    """参数优化聚合结果。

    Attributes:
        strategy_name: 策略注册名。
        objective: 目标指标名。
        trials: 聚合结果字典列表（含错误组；KP-P2-05 起每条含 in_*/out_* 双区段指标）。
        elapsed_seconds: 并行评估总耗时。
        split_mode: 是否启用样本内→样本外分割（in_sample_end 已设置）。
        evaluation_count: 试验次数（实际执行的回测数；分割模式=参数组合×2）。
    """

    strategy_name: str
    objective: str
    trials: List[Dict[str, Any]]
    elapsed_seconds: float
    split_mode: bool = False
    evaluation_count: int = 0

    # ── 派生统计（惰性缓存字段） ──
    _completed: Optional[List[Dict[str, Any]]] = None
    _error_count: Optional[int] = None

    @property
    def total_count(self) -> int:
        """参数组合总数。"""
        return len(self.trials)

    @property
    def completed_count(self) -> int:
        """成功完成任务数。"""
        return len(self._completed_outcomes())

    @property
    def error_count(self) -> int:
        """失败任务数。"""
        if self._error_count is None:
            self._error_count = sum(
                1 for t in self.trials if t.get("status") != "completed"
            )
        return self._error_count

    def _completed_outcomes(self) -> List[Dict[str, Any]]:
        """缓存成功评估结果列表。"""
        if self._completed is None:
            self._completed = [
                t for t in self.trials if t.get("status") == "completed"
            ]
        return self._completed

    def _score(self, outcome: Dict[str, Any], prefix: str = "") -> Optional[float]:
        """按目标指标对单条结果打分；无该指标返回 None。

        Args:
            outcome: 聚合结果记录。
            prefix: 指标字段前缀：""=结论字段（分割模式下为样本外）、
                "in_"=样本内、 "out_"=样本外。
        """
        value = outcome.get(f"{prefix}{self.objective}")
        if value is None:
            return None
        return float(value)

    def _rank(
        self,
        outcomes: List[Dict[str, Any]],
        prefix: str,
        top: int,
    ) -> List[Dict[str, Any]]:
        """按指定前缀的目标指标对结果排序，返回前 top 名（无指标恒排最后）。

        Args:
            outcomes: 待排序的聚合结果列表。
            prefix: 指标字段前缀（""/"in_"/"out_"）。
            top: 返回条数上限（非负整数）。

        Returns:
            排序后的结果字典列表，每条附加 "_rank" 排名。
        """
        # P1-16（#366）：聚合排序前校验 manifest 签名一致——跨真实性配置
        # （不同策略池/数据口径/参数范围）禁止合并排行（§4.10）。
        assert_uniform_signature(outcomes, context="参数优化聚合排行")
        direction = OBJECTIVE_DIRECTION[self.objective]
        scored = [(t, self._score(t, prefix)) for t in outcomes]
        # 有分值的按方向排序在前，None（失败/缺指标）按原始顺序排最后
        keyed = [(t, s, -s if (s is not None and direction) else s) for t, s in scored]
        ordered = sorted(
            keyed,
            key=lambda x: (x[1] is None, x[2] if x[1] is not None else 0),
            reverse=False,
        )
        ranked_list: List[Dict[str, Any]] = []
        for rank, (t, _s, _k) in enumerate(ordered[:top], start=1):
            item = dict(t)
            item["_rank"] = rank
            ranked_list.append(item)
        return ranked_list

    def ranked(self, top: int = RANK_TOP_N) -> List[Dict[str, Any]]:
        """按结论指标排序返回前 top 名（失败组恒排最后）。

        分割模式下结论指标=样本外（"结论以样本外为准"）；无分割时=全样本指标。

        Args:
            top: 返回条数上限（非负整数）。

        Returns:
            排序后的结果字典列表，每条附加 "_rank" 排名。
        """
        if top < 0:
            raise ValueError(f"top 必须为非负整数，收到: {top!r}")
        return self._rank(self._completed_outcomes(), "", top)

    def ranked_in_sample(self, top: int = RANK_TOP_N) -> List[Dict[str, Any]]:
        """按样本内指标排序返回前 top 名（寻优依据）。

        无分割模式下等价于 ranked()（此时无样本外概念）。

        Args:
            top: 返回条数上限（非负整数）。

        Returns:
            排序后的结果字典列表，每条附加 "_rank" 排名。
        """
        if top < 0:
            raise ValueError(f"top 必须为非负整数，收到: {top!r}")
        if not self.split_mode:
            return self.ranked(top=top)
        return self._rank(self._completed_outcomes(), "in_", top)

    def ranked_out_sample(self, top: int = RANK_TOP_N) -> List[Dict[str, Any]]:
        """按样本外指标排序返回前 top 名（确认依据）。

        无分割模式（未启用样本外）下返回空列表。

        Args:
            top: 返回条数上限（非负整数）。

        Returns:
            排序后的结果字典列表，每条附加 "_rank" 排名。
        """
        if top < 0:
            raise ValueError(f"top 必须为非负整数，收到: {top!r}")
        if not self.split_mode:
            return []
        return self._rank(self._completed_outcomes(), "out_", top)

    def best(self) -> Optional[Dict[str, Any]]:
        """返回最优参数组合（无成功结果时返回 None）。

        分割模式下：样本内选参（ranked_in_sample 取顶），返回的记录含 in_*/out_* 双列，
        结论字段（sharpe_ratio 等）为样本外——即"同一网格在样本内选中后在样本外复算"；
        无分割时按结论指标取顶（兼容原行为）。
        """
        if self.completed_count == 0:
            return None
        if self.split_mode:
            return self.ranked_in_sample(top=1)[0]
        return self.ranked(top=1)[0]

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可读字典（供 JSON 落盘）。"""
        return {
            "strategy_name": self.strategy_name,
            "objective": self.objective,
            "split_mode": self.split_mode,
            "total_count": self.total_count,
            "evaluation_count": self.evaluation_count,
            "completed_count": self.completed_count,
            "error_count": self.error_count,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "best": self.best(),
            "trials": self.trials,
        }

    def to_dataframe(self) -> pd.DataFrame:
        """转 DataFrame：每行一条结果，params 展开为独立列，失败组指标置空。

        分割模式额外输出 in_*/out_* 双区段列（样本内寻优 / 样本外确认）。
        """
        rows: List[Dict[str, Any]] = []
        for t in self.trials:
            row = {
                "symbol": t.get("symbol"),
                "status": t.get("status"),
                "total_return": t.get("total_return"),
                "sharpe_ratio": t.get("sharpe_ratio"),
                "max_drawdown": t.get("max_drawdown"),
                "trade_count": t.get("trade_count"),
                "error": t.get("error"),
            }
            if self.split_mode:
                for metric in _CONCLUSION_METRICS:
                    row[f"in_{metric}"] = t.get(f"in_{metric}")
                    row[f"out_{metric}"] = t.get(f"out_{metric}")
            for k, v in (t.get("params") or {}).items():
                row[f"param_{k}"] = v
            rows.append(row)
        return pd.DataFrame(rows)

    def save_csv(self, output_path: str) -> str:
        """P2-13：把千次实验聚合明细落盘为中文表头 CSV（一行一条参数组合）。

        复用 to_dataframe + csv_utils.export_csv_cn，params 展开为 param_*→参数:*
        中文列；失败组指标留空。父目录自动创建，编码 utf-8-sig（Excel 直开不乱码）。

        Args:
            output_path: 输出 CSV 路径。

        Returns:
            str: 实际写入路径。
        """
        from pathlib import Path

        from myquant.utils.csv_utils import (
            CSV_HEADER_MAPS,
            PARAM_COLUMN_CN_PREFIX,
            PARAM_COLUMN_PREFIX,
            export_csv_cn,
        )

        df = self.to_dataframe()
        if df.empty:
            logger.warning("参数优化结果为空，未落盘: %s", output_path)
            return str(Path(output_path))
        out = export_csv_cn(
            df,
            output_path,
            header_map=CSV_HEADER_MAPS["param_optimize"],
            prefix_map={PARAM_COLUMN_PREFIX: PARAM_COLUMN_CN_PREFIX},
        )
        logger.info("参数优化千次实验明细已落盘: %s (%d 行)", out, len(df))
        return str(out)

    def summary(self) -> str:
        """生成优化汇总报告文本（含最优参数与 Top 榜；分割模式输出 in/out 双列）。"""
        lines = ["策略参数优化报告"]
        lines.append("=" * 64)
        mode = "样本内→样本外" if self.split_mode else "全样本"
        lines.append(f"策略: {self.strategy_name} | 目标: {self.objective} | 分割: {mode}")
        lines.append(
            f"参数组合: {self.total_count}（成功 {self.completed_count} / "
            f"失败 {self.error_count}）| 试验次数: {self.evaluation_count} | "
            f"评估耗时: {format_duration(self.elapsed_seconds)}"
        )
        best = self.best()
        if best is not None:
            lines.append("-" * 64)
            if self.split_mode:
                lines.append("最优参数组合（样本内寻优，结论以样本外为准）:")
                lines.append(f"  {best.get('params')}")
                lines.append(
                    f"  样本内: 收益 {float(best.get('in_total_return') or 0):.2%} | "
                    f"夏普 {float(best.get('in_sharpe_ratio') or 0):.3f} | "
                    f"回撤 {float(best.get('in_max_drawdown') or 0):.2%}"
                )
                lines.append(
                    f"  样本外: 收益 {float(best.get('out_total_return') or 0):.2%} | "
                    f"夏普 {float(best.get('out_sharpe_ratio') or 0):.3f} | "
                    f"回撤 {float(best.get('out_max_drawdown') or 0):.2%}"
                )
            else:
                lines.append("最优参数组合:")
                lines.append(f"  {best.get('params')}")
                lines.append(
                    f"  总收益 {float(best.get('total_return') or 0):.2%} | "
                    f"夏普 {float(best.get('sharpe_ratio') or 0):.3f} | "
                    f"最大回撤 {float(best.get('max_drawdown') or 0):.2%} | "
                    f"交易数 {best.get('trade_count')}"
                )
        top = self.ranked_in_sample(top=RANK_TOP_N)
        if top:
            lines.append("-" * 64)
            if self.split_mode:
                lines.append(f"Top {len(top)} 样本内排名（寻优）:")
                lines.append(
                    f"{'排名':<5} {'标的':<10} {'参数':<28} {'样本内夏普':>10} {'样本外夏普':>10}"
                )
                for item in top:
                    lines.append(
                        f"{item['_rank']:<5} {str(item.get('symbol')):<10} "
                        f"{str(item.get('params')):<28} "
                        f"{float(item.get('in_sharpe_ratio') or 0):>10.3f} "
                        f"{float(item.get('out_sharpe_ratio') or 0):>10.3f}"
                    )
            else:
                lines.append(f"Top {len(top)} 排名:")
                lines.append(
                    f"{'排名':<5} {'标的':<10} {'参数':<28} {'夏普':>8} {'收益':>8} {'回撤':>8}"
                )
                for item in top:
                    lines.append(
                        f"{item['_rank']:<5} {str(item.get('symbol')):<10} "
                        f"{str(item.get('params')):<28} "
                        f"{float(item.get('sharpe_ratio') or 0):>8.3f} "
                        f"{float(item.get('total_return') or 0):>8.2%} "
                        f"{float(item.get('max_drawdown') or 0):>8.2%}"
                    )
        return "\n".join(lines)


@dataclass
class WalkForwardResult:
    """滚动 walk-forward 三段式验证聚合结果（KP-P2-05 第三阶段）。

    Attributes:
        strategy_name: 策略注册名。
        objective: 目标指标名。
        folds: 每折验证结果列表（含训练截止/测试截止/最优参数/in-out 样本外指标）。
    """

    strategy_name: str
    objective: str
    folds: List[Dict[str, Any]]

    @property
    def fold_count(self) -> int:
        """滚动折数。"""
        return len(self.folds)

    def summary(self) -> str:
        """生成滚动 walk-forward 验证报告文本。"""
        lines = ["滚动 walk-forward 验证报告"]
        lines.append("=" * 64)
        lines.append(
            f"策略: {self.strategy_name} | 目标: {self.objective} | 折数: {len(self.folds)}"
        )
        lines.append(
            f"{'折':<4} {'训练截止':<12} {'测试截止':<12} "
            f"{'样本内夏普':>10} {'样本外夏普':>10} {'样本外收益':>10}"
        )
        valid: List[Dict[str, Any]] = []
        for fold in self.folds:
            out_sr = fold.get("out_sharpe_ratio")
            if out_sr is not None:
                valid.append(fold)
            lines.append(
                f"{fold['fold_index']:<4} {fold['train_end']:<12} {fold['test_end']:<12} "
                f"{float(fold.get('in_sharpe_ratio') or 0):>10.3f} "
                f"{float(out_sr or 0):>10.3f} "
                f"{float(fold.get('out_total_return') or 0):>10.2%}"
            )
        if valid:
            avg_out = sum(float(f["out_sharpe_ratio"]) for f in valid) / len(valid)
            lines.append("-" * 64)
            lines.append(f"样本外平均夏普（{len(valid)} 折有效）: {avg_out:.3f}（结论以样本外为准）")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可读字典（供 JSON 落盘）。"""
        return {
            "strategy_name": self.strategy_name,
            "objective": self.objective,
            "fold_count": len(self.folds),
            "folds": self.folds,
        }


# ── 优化器 ────────────────────────────────────────────────────────────
def _write_param_opt_leakage_report(opt: "ParamOptimizer") -> None:
    """参数优化出口泄漏审计报告落盘（P1-11 步骤③，R-16 §4.4）。

    decision_time 取样本内截止（in_sample_end，最近真实决策时点）或回测结束日
    （end_date，P2-11 口径）；lookahead_guard 统计为进程内累计快照（并行子进程
    各自维护单例，主进程统计语义为"当前进程已记录"）。报告失败不阻断优化。

    Args:
        opt: 已执行 optimize() 的优化器实例。
    """
    from myquant.core.lookahead_guard import get_lookahead_guard
    from myquant.leakage_gate.gate import collect_gate_stats
    from myquant.leakage_gate.report import write_experiment_report

    decision_time = opt.end_date or opt.in_sample_end or ""
    strategy_id = opt.strategy_name
    try:
        gate_stats = collect_gate_stats(strategy_id, decision_time)
        write_experiment_report(
            experiment_id=f"param_opt_{opt.start_date}_{decision_time}",
            experiment_type="param_optimization",
            strategy_id=strategy_id,
            decision_time=decision_time,
            gate_stats=gate_stats,
            lookahead_guard=get_lookahead_guard().stats,
        )
    except Exception:
        # 报告是审计产物：失败仅记录可见，不得阻断参数优化（py 规则：记录后恢复）
        logger.warning(
            "参数优化泄漏审计报告落盘失败（P1-11，审计旁路不阻断优化）", exc_info=True)


class ParamOptimizer:
    """策略参数网格搜索优化器（进程池并行评估）。

    Args:
        strategy_name: 策略注册名（见 STRATEGY_REGISTRY）。
        grid: 参数网格。
        data: 多标的行情字典 {symbol: DataFrame}（含 close 列）。
        objective: 目标指标（见 OBJECTIVE_DIRECTION）。
        cpu_workers: 计算进程池大小；None 使用默认。
        start_date: 回测起始日期。
        end_date: 回测结束日期。
        in_sample_end: 样本内截止日期（YYYY-MM-DD）；设置后启用样本内→样本外分割
            （KP-P2-05：样本内寻优、样本外确认，报告输出 in/out 双列）。
        initial_capital: 初始资金（>0）。
        frequency: K 线频率。

    Example:
        >>> grid = ParamGrid((ParamSpec("fast_period", (3, 5, 8)),))
        >>> opt = ParamOptimizer("ma_cross", grid, data)
        >>> result = opt.optimize()
    """

    def __init__(
        self,
        strategy_name: str,
        grid: ParamGrid,
        data: Dict[str, pd.DataFrame],
        objective: str = DEFAULT_OBJECTIVE,
        cpu_workers: Optional[int] = None,
        start_date: str = "",
        end_date: str = "",
        in_sample_end: str = "",
        initial_capital: float = DEFAULT_INITIAL_CAPITAL,
        frequency: str = DEFAULT_FREQUENCY,
        broker_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        """初始化优化器并做防御式参数校验。

        Args:
            broker_factory: 撮合执行器工厂（G1 EX-09A 注入点，签名
                factory(config) -> broker；None 时引擎回退全局
                register_broker_factory，未装配则显式报错）。"""
        if not isinstance(strategy_name, str) or not strategy_name.strip():
            raise ValueError("strategy_name 必须为非空字符串")
        if strategy_name not in STRATEGY_REGISTRY:
            raise ValueError(
                f"未知策略: {strategy_name!r}，已注册: {sorted(STRATEGY_REGISTRY)}"
            )
        if not isinstance(grid, ParamGrid):
            raise TypeError(f"grid 必须为 ParamGrid，收到: {type(grid).__name__}")
        if not isinstance(data, dict) or len(data) == 0:
            raise ValueError("data 必须为非空字典 {symbol: DataFrame}")
        if objective not in OBJECTIVE_DIRECTION:
            raise ValueError(
                f"未知目标指标: {objective!r}，可选: {sorted(OBJECTIVE_DIRECTION)}"
            )
        if cpu_workers is not None and (not isinstance(cpu_workers, int) or cpu_workers < 1):
            raise ValueError(f"cpu_workers 必须为正整数或 None，收到: {cpu_workers!r}")
        if not isinstance(initial_capital, (int, float)) or initial_capital <= 0:
            raise ValueError(f"initial_capital 必须为正数，收到: {initial_capital!r}")
        if not isinstance(frequency, str) or not frequency.strip():
            raise ValueError("frequency 必须为非空字符串")
        if not isinstance(in_sample_end, str):
            raise TypeError(
                f"in_sample_end 必须为字符串，收到: {type(in_sample_end).__name__}"
            )
        # KP-P2-05：in_sample_end 必须严格落在 [start, end] 开区间内，否则无法分割
        if in_sample_end:
            if not start_date or not end_date:
                raise ValueError(
                    "设置 in_sample_end 时必须同时提供 start_date 与 end_date"
                )
            if not (start_date < in_sample_end < end_date):
                raise ValueError(
                    f"in_sample_end 必须介于 start_date 与 end_date 之间: "
                    f"{start_date} < {in_sample_end} < {end_date}"
                )

        # 校验行情列（防御：缺 OHLCV 列导致子进程集体失败；中文列优先，兼容英文列）
        for symbol, df in data.items():
            if not isinstance(df, pd.DataFrame):
                raise TypeError(f"标的 {symbol} 的数据必须为 DataFrame")
            require_ohlcv(df, symbol=symbol)

        self.strategy_name = strategy_name
        self.grid = grid
        self.data = data
        self.objective = objective
        self.cpu_workers = cpu_workers
        self.start_date = start_date
        self.end_date = end_date
        self.in_sample_end = in_sample_end
        self.initial_capital = initial_capital
        self.frequency = frequency
        self._broker_factory: Optional[Callable[[Any], Any]] = broker_factory

    def _build_trials(self) -> List[ParamTrial]:
        """展开全部任务：参数组合 x 标的，每任务只携带本标的行情。

        启用 in_sample_end 时每个组合拆为两个区段任务（split="in"/"out"），
        样本内寻优、样本外确认（KP-P2-05）；未启用时单任务（split="in"，兼容原行为）。
        """
        trials: List[ParamTrial] = []
        split_mode = bool(self.in_sample_end)
        for combo in self.grid.combinations():
            for symbol, df in self.data.items():
                if split_mode:
                    trials.append(
                        ParamTrial(
                            params=combo,
                            strategy_name=self.strategy_name,
                            symbol=symbol,
                            data={symbol: df},
                            start_date=self.start_date,
                            end_date=self.in_sample_end,
                            initial_capital=self.initial_capital,
                            frequency=self.frequency,
                            split="in",
                        )
                    )
                    trials.append(
                        ParamTrial(
                            params=combo,
                            strategy_name=self.strategy_name,
                            symbol=symbol,
                            data={symbol: df},
                            start_date=self.in_sample_end,
                            end_date=self.end_date,
                            initial_capital=self.initial_capital,
                            frequency=self.frequency,
                            split="out",
                        )
                    )
                else:
                    trials.append(
                        ParamTrial(
                            params=combo,
                            strategy_name=self.strategy_name,
                            symbol=symbol,
                            data={symbol: df},
                            start_date=self.start_date,
                            end_date=self.end_date,
                            initial_capital=self.initial_capital,
                            frequency=self.frequency,
                            split="in",
                        )
                    )
        return trials

    def optimize(self) -> ParamOptimizationResult:
        """执行并行网格搜索。

        并行上下文只包裹评估段：进入自动关闭 factor_cache L1 并告警，
        退出自动恢复；进程池提交前做 pickle 预检拦截不可序列化任务。

        Returns:
            聚合结果（含最优参数、Top 榜、耗时）。
        """
        trials = self._build_trials()
        total = len(trials)
        if total == 0:
            raise ValueError("参数网格展开后无任何任务（数据或网格为空）")

        logger.info("参数优化启动: %d 组任务（%d 参数组合 x %d 标的）",
                    total, len(self.grid), len(self.data))

        def _progress(done: int, count: int) -> None:
            """进度回显（仅 logging.info，避免刷屏）。"""
            logger.info("并行评估进度: %d/%d", done, count)

        # 并行上下文：自动管理缓存开关（L1 关闭 + 告警），退出自动恢复
        eval_t0 = time.perf_counter()
        with parallel_factor_context(cpu_workers=self.cpu_workers) as pm:
            # G1 EX-09A：主进程注册 + 任务函数经 partial 显式传工厂（
            # Windows spawn 下子进程 pickle partial 参数，不依赖全局注册表
            # 与 initializer——共享进程池单例场景下后者不可靠）
            if self._broker_factory is not None:
                register_broker_factory(self._broker_factory)
            task_fn: Any = evaluate_params
            if self._broker_factory is not None:
                task_fn = partial(evaluate_params, broker_factory=self._broker_factory)
            outcomes = pm.map_cpu(
                task_fn,
                trials,
                return_exceptions=True,
                check_pickle=True,
                progress_cb=_progress,
            )
        elapsed = time.perf_counter() - eval_t0

        # 归一化：异常对象（兜底）转为 error 结果字典
        normalized: List[Dict[str, Any]] = []
        for trial, outcome in zip(trials, outcomes):
            if isinstance(outcome, BaseException):
                normalized.append(
                    {
                        "params": trial.params,
                        "symbol": trial.symbol,
                        "split": trial.split,
                        "status": "error",
                        "total_return": None,
                        "sharpe_ratio": None,
                        "max_drawdown": None,
                        "trade_count": None,
                        "error": f"{type(outcome).__name__}: {outcome}",
                    }
                )
            else:
                normalized.append(outcome)

        errors = sum(1 for o in normalized if o.get("status") != "completed")
        if errors:
            logger.warning("参数优化完成: %d/%d 组区段失败（已隔离，不影响其余）",
                           errors, total)

        # KP-P2-05：按 (params, symbol) 聚合 in/out 双区段为单条记录（含 in_*/out_* 双列）
        combined = _coalesce_outcomes(normalized)

        # P1-11：参数优化出口泄漏审计报告（R-16 §4.4；P1-04 #351 收敛后
        # 每 trial 已走 create_engine → 门禁断言已在引擎创建时执行，此处仅落报告）
        _write_param_opt_leakage_report(self)

        return ParamOptimizationResult(
            strategy_name=self.strategy_name,
            objective=self.objective,
            trials=combined,
            elapsed_seconds=elapsed,
            split_mode=bool(self.in_sample_end),
            evaluation_count=len(normalized),
        )

    def walk_forward(self, folds: int = 3) -> WalkForwardResult:
        """滚动 walk-forward 三段式验证（KP-P2-05 第三阶段）。

        把全局数据区间滚动切成 ``folds`` 个样本外窗口：每个折用【全局起点→该折训练截止】
        的数据做样本内寻优（复用 optimize 的 in/out 分割），在该折测试窗口做样本外确认；
        聚合所有折的样本外绩效作为抗过拟合的最终结论。

        时间序列计算仅使用当前折训练截止（含）及之前的已可得数据，无未来函数。

        Args:
            folds: 滚动折数（>=2）。每折测试窗口大小 = 可用样本外 / folds。

        Returns:
            每折验证结果聚合（含样本外平均绩效）。

        Raises:
            ValueError: folds 非法或数据区间过短。
        """
        if not isinstance(folds, int) or isinstance(folds, bool) or folds < 2:
            raise ValueError(f"folds 必须为 >=2 的整数，收到: {folds!r}")
        # 从任一标的数据 index 取全局可用日期（升序去重）
        index = next(iter(self.data.values())).index
        dates: List = sorted(pd.Index(index).unique())
        n = len(dates)
        if n < (folds + 1):
            raise ValueError(
                f"数据区间过短，无法做 {folds} 折 walk-forward（可用日期仅 {n} 根）"
            )
        # 最小训练长度占 1/3，保证每折训练段有预热与寻优意义
        min_train = max(1, n // 3)
        available_test = n - min_train
        if available_test < folds:
            raise ValueError(
                f"可用样本外过短（{available_test} 根 < 折数 {folds}），请减小 folds"
            )
        test_size = available_test // folds

        fold_results: List[Dict[str, Any]] = []
        global_start = dates[0].strftime("%Y-%m-%d")
        for i in range(folds):
            test_start_idx = min_train + i * test_size
            test_end_idx = min_train + (i + 1) * test_size
            if i == folds - 1:
                test_end_idx = n - 1
            train_end = dates[test_start_idx].strftime("%Y-%m-%d")
            test_end = dates[test_end_idx].strftime("%Y-%m-%d")
            sub_opt = ParamOptimizer(
                strategy_name=self.strategy_name,
                grid=self.grid,
                data=self.data,
                objective=self.objective,
                cpu_workers=self.cpu_workers,
                start_date=global_start,
                end_date=test_end,
                in_sample_end=train_end,
                initial_capital=self.initial_capital,
                frequency=self.frequency,
                broker_factory=self._broker_factory,
            )
            result = sub_opt.optimize()
            best = result.best()
            fold_results.append(
                {
                    "fold_index": i + 1,
                    "train_end": train_end,
                    "test_end": test_end,
                    "best_params": best.get("params") if best is not None else None,
                    "in_sharpe_ratio": (
                        best.get("in_sharpe_ratio") if best is not None else None
                    ),
                    "out_sharpe_ratio": (
                        best.get("out_sharpe_ratio") if best is not None else None
                    ),
                    "out_total_return": (
                        best.get("out_total_return") if best is not None else None
                    ),
                }
            )
        return WalkForwardResult(
            strategy_name=self.strategy_name,
            objective=self.objective,
            folds=fold_results,
        )


# ── 演示 / 自测入口 ───────────────────────────────────────────────────
def make_demo_data(
    symbols: Sequence[str] = ("000001.SZ", "000002.SZ", "600000.SH"),
    n_bars: int = 300,
    seed: int = 42,
    seed_sidecar_path: Optional[str] = None,
) -> Dict[str, pd.DataFrame]:
    """构造确定性演示行情（趋势 + 周期噪声），供脚本与自测使用。

    Args:
        symbols: 标的代码序列。
        n_bars: 每标的 K 线数量（>20）。
        seed: 随机种子（同 seed 结果可复现）。
        seed_sidecar_path: M2-06 种子 sidecar 路径；非 None 时校验/记录种子
            （种子不一致 → 阻断），保证参数优化结果可复现、种子留痕。

    Returns:
        {symbol: DataFrame}，列为 open/high/low/close/volume/amount，
        index 为工作日 DatetimeIndex。

    Raises:
        ValueError: n_bars 过小。
        SeedSidecarError: 种子 sidecar 不一致（M2-06 阻断）。
    """
    if n_bars < 20:
        raise ValueError(f"n_bars 至少 20 根以便均线预热，收到: {n_bars}")
    # M2-06：种子 sidecar 校验/记录（种子不一致 → 阻断）
    if seed_sidecar_path is not None:
        from myquant.leakage_gate.sanity import SeedSidecar
        SeedSidecar(seed_sidecar_path).check_or_record("param_optimizer", seed)
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-02", periods=n_bars, freq="B")
    data: Dict[str, pd.DataFrame] = {}
    for idx, symbol in enumerate(symbols):
        drift = 0.0006 + 0.0004 * idx
        noise = rng.normal(0.0, 0.01, n_bars)
        trend = drift * np.arange(n_bars)
        wave = 0.012 * np.sin(np.arange(n_bars) / (18.0 + idx))
        closes = 10.0 * (1.0 + trend + noise + wave)
        data[symbol] = pd.DataFrame(
            {
                "open": closes * 0.997,
                "high": closes * 1.01,
                "low": closes * 0.99,
                "close": closes,
                "volume": 100_000.0 * (1.0 + 0.5 * rng.random(n_bars)),
                "amount": closes * 100_000.0,
            },
            index=dates,
        )
    return data


def _main() -> int:
    """模块自测：小网格 + 3 标的，验证并行接入与最优参数输出。"""
    import time

    print("=" * 64)
    print("  param_optimizer.py — 参数优化（并行接入）自测 Demo")
    print("=" * 64)

    # 1. 构造演示数据（确定性）
    data = make_demo_data(n_bars=300, seed=7)

    # 2. 参数网格：fast/slow 双均线
    grid = ParamGrid(
        (
            ParamSpec("fast_period", (3, 5, 8, 12)),
            ParamSpec("slow_period", (15, 25, 40)),
        )
    )
    print(f"[数据] {len(data)} 标的，每标的 {300} 根 K 线")
    print(f"[网格] {len(grid)} 组参数组合 x {len(data)} 标的 = "
          f"{len(grid) * len(data)} 组任务")

    # 3. 执行优化（parallel_factor_context 自动管理缓存开关）
    t0 = time.perf_counter()
    optimizer = ParamOptimizer(
        strategy_name="ma_cross",
        grid=grid,
        data=data,
        objective="sharpe_ratio",
        start_date="2023-01-02",
        end_date="2024-01-31",
    )
    result = optimizer.optimize()
    elapsed = time.perf_counter() - t0

    # 4. 输出
    print(result.summary())
    best = result.best()
    assert best is not None, "应有最优参数"
    assert best["status"] == "completed", "最优参数应来自成功组"
    assert result.completed_count + result.error_count == result.total_count
    assert all(t["status"] in ("completed", "error") for t in result.trials)
    print(f"[总耗时] {elapsed:.3f}s（并行评估 {result.elapsed_seconds:.3f}s）")
    print("=" * 64)
    print(f"  自测通过: {result.total_count} 组任务，"
          f"成功 {result.completed_count} / 失败 {result.error_count}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
