# 文件名：myquant/ml/inference_benchmark.py
"""
inference_benchmark.py - 推理延迟基准（F-06）
============================================
背景（问题链，审查确认）:
  - ML 层缺延迟达标证明：推理服务 predict_realtime 耗时未量化，
    分钟级策略无 P99 阈值依据（任务卡：P99 < 500ms，初始目标 < 200ms）；
  - 缺"随历史长度增长"压测：输入历史越长耗时如何变化无数据。

本模块职责:
  - InferenceBenchmark.measure()：合成数据（随机游走防未来）训练内置模型
    到独立临时 registry → 对递增历史长度（100/250/500/1000/2000）压测
    predict_realtime 全链路（compute_incremental + 模型 predict）
    → 输出各长度 P50/P95/P99/均值/最大 + 达标判定；
  - 阈值双档：hard_limit_ms=500（passed 判定）/ target_ms=200（初始目标标记）；
  - 报告 dict JSON 可序列化，供 scripts/推理延迟基准.py 入档。

防未来函数（铁律 11）与合规要点:
  - 全部数据为确定性随机游走合成（零网络、零实盘），不连实盘；
  - 压测序列 timestamp 起始晚于训练截止（推理只发生在训练后的真未来，
    无样本内告警）；
  - 独立临时 registry（不碰 datasets/models 真实模型）。
"""

from __future__ import annotations

import platform
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.ml.features import FeatureConfig
from myquant.ml.inference_service import InferenceService
from myquant.ml.model_registry import ModelRegistry
from myquant.ml.training_pipeline import TrainingPipeline

logger = get_logger("ml.inference_benchmark")


# ═══════════════════════════════════════════════
#  常量（集中定义，禁止魔法数字；可经构造参数覆盖）
# ═══════════════════════════════════════════════

# 压测历史长度递增序列（任务卡：随历史长度增长）
DEFAULT_LENGTHS: List[int] = [100, 250, 500, 1000, 2000]
# 每长度重复次数（取百分位统计稳定性）
DEFAULT_REPEAT: int = 5
# 合成数据随机种子（确定性可复现）
DEFAULT_SEED: int = 42
# 训练用行数（保证特征窗口 + 时间序列切分充足）
TRAIN_ROWS: int = 2000
# 分钟级策略硬阈值：P99 < 500ms 判定达标（任务卡）
HARD_LIMIT_MS: float = 500.0
# 初始目标：P99 < 200ms（任务卡"初始目标"标记）
TARGET_MS: float = 200.0


# ═══════════════════════════════════════════════
#  合成数据（随机游走，防未来）
# ═══════════════════════════════════════════════

def _synthetic_ohlcv(n_rows: int, seed: int, ts_offset: int = 0) -> pd.DataFrame:
    """确定性随机游走 OHLCV（中文列契约 + timestamp 列）。

    本行使用的数据截至 t 时刻：纯合成数据，不引用任何真实行情/未来数据。

    Args:
        n_rows: 行数。
        seed: 随机种子（可复现）。
        ts_offset: timestamp 起始偏移（压测序列晚于训练截止，防样本内）。

    Returns:
        中文列 OHLCV DataFrame（timestamp/开盘价/最高价/最低价/收盘价/成交量/成交额）。
    """
    if n_rows < 1:
        raise ValueError(f"n_rows 必须 >=1，got: {n_rows}")
    rng = np.random.default_rng(seed)
    closes = [10.0]
    for _ in range(n_rows - 1):
        closes.append(closes[-1] * (1 + rng.normal(0, 0.02)))
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.01, n_rows)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.01, n_rows)))
    volume = rng.uniform(1000.0, 5000.0, n_rows)
    return pd.DataFrame({
        "timestamp": 1700000000 + ts_offset + np.arange(n_rows, dtype=float),
        "开盘价": opens,
        "最高价": highs,
        "最低价": lows,
        "收盘价": closes,
        "成交量": volume,
        "成交额": closes * volume,
    })


# ═══════════════════════════════════════════════
#  LengthResult 单长度压测结果
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class LengthResult:
    """单个历史长度的推理延迟统计。

    Attributes:
        length: 输入历史行数。
        p50_ms / p95_ms / p99_ms: 延迟百分位（毫秒）。
        mean_ms / max_ms: 均值 / 最大值（毫秒）。
        passed: 是否达标（p99_ms < hard_limit_ms）。
    """

    length: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    max_ms: float
    passed: bool

    def to_dict(self) -> Dict[str, Any]:
        """序列化（JSON 安全）。"""
        return {
            "length": self.length,
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "passed": self.passed,
        }


# ═══════════════════════════════════════════════
#  InferenceBenchmark 推理延迟基准
# ═══════════════════════════════════════════════

class InferenceBenchmark:
    """推理延迟基准（F-06）：predict_realtime 全链路压测。

    用法:
      bench = InferenceBenchmark()
      report = bench.measure()
      # report 可 JSON 落盘，含各长度 P99 与达标判定
    """

    def __init__(
        self,
        hard_limit_ms: float = HARD_LIMIT_MS,
        target_ms: float = TARGET_MS,
        repeat: int = DEFAULT_REPEAT,
        seed: int = DEFAULT_SEED,
    ):
        """初始化基准。

        Args:
            hard_limit_ms: 硬阈值（P99 低于此值 passed=True）。
            target_ms: 初始目标（报告标记，不参与判定）。
            repeat: 每长度重复次数（>=1）。
            seed: 合成数据种子。

        Raises:
            ValueError: 参数非法时抛出。
        """
        if not isinstance(hard_limit_ms, (int, float)) or hard_limit_ms <= 0:
            raise ValueError(f"hard_limit_ms 必须 >0，got: {hard_limit_ms!r}")
        if not isinstance(target_ms, (int, float)) or target_ms <= 0:
            raise ValueError(f"target_ms 必须 >0，got: {target_ms!r}")
        if not isinstance(repeat, int) or repeat < 1:
            raise ValueError(f"repeat 必须 >=1 的整数，got: {repeat!r}")
        self.hard_limit_ms = float(hard_limit_ms)
        self.target_ms = float(target_ms)
        self.repeat = repeat
        self.seed = seed

    # ─────────────────────────────────────────────
    #  训练（独立临时 registry）
    # ─────────────────────────────────────────────

    @staticmethod
    def _train(
        registry: ModelRegistry,
        model_name: str,
        seed: int,
    ) -> InferenceService:
        """合成数据训练内置模型并注册，返回推理服务。

        防未来：训练数据为确定性随机游走（无真实行情/网络）；
        独立临时 registry，不碰 datasets/models 真实模型。
        """
        data = _synthetic_ohlcv(TRAIN_ROWS, seed=seed)
        pipeline = TrainingPipeline(registry=registry)
        pipeline.run(data, model_name, source="offline",
                     description="F-06 延迟基准合成模型")
        return InferenceService(registry=registry)

    @staticmethod
    def _validate_lengths(lengths: Sequence[int]) -> List[int]:
        """压测长度校验（非空、升序、正整数）。"""
        if not lengths:
            raise ValueError("lengths 不能为空")
        out: List[int] = []
        for raw in lengths:
            if not isinstance(raw, int) or raw < 1:
                raise ValueError(f"lengths 必须为正整数，got: {raw!r}")
            out.append(raw)
        if out != sorted(out):
            raise ValueError(f"lengths 必须升序，got: {out}")
        return out

    # ─────────────────────────────────────────────
    #  测量
    # ─────────────────────────────────────────────

    def measure(
        self,
        lengths: Optional[Sequence[int]] = None,
        model_name: str = "benchmark_model",
        registry: Optional[ModelRegistry] = None,
    ) -> Dict[str, Any]:
        """执行延迟基准压测。

        Args:
            lengths: 压测历史长度序列（默认 DEFAULT_LENGTHS，升序）。
            model_name: 模型名（训练并注册用）。
            registry: 外部 registry（默认创建独立临时目录，
                不碰 datasets/models）。

        Returns:
            报告 dict（JSON 可序列化）：
              environment / hard_limit_ms / target_ms / repeat /
              lengths（LengthResult.to_dict 列表）/ overall_passed / note。

        Raises:
            ValueError: lengths 非法时抛出。
        """
        lengths = self._validate_lengths(lengths or DEFAULT_LENGTHS)
        own_registry = registry is None
        if own_registry:
            tmp_dir = tempfile.mkdtemp(prefix="ml_bench_")
            registry = ModelRegistry(models_root=str(Path(tmp_dir) / "models"))

        try:
            service = self._train(registry, model_name, seed=self.seed)
            results: List[LengthResult] = []
            for i, length in enumerate(lengths):
                # 压测序列 timestamp 晚于训练截止（防样本内告警）；
                # 每次独立合成（seed 偏移，确定性）。
                data = _synthetic_ohlcv(
                    length, seed=self.seed + 1000 + i,
                    ts_offset=TRAIN_ROWS + 10_000_000,
                )
                elapsed_ms: List[float] = []
                for _ in range(self.repeat):
                    t0 = time.perf_counter()
                    service.predict_realtime(data, model_name, strict=False)
                    elapsed_ms.append((time.perf_counter() - t0) * 1000.0)
                arr = np.asarray(elapsed_ms, dtype=float)
                p50 = float(np.percentile(arr, 50))
                p95 = float(np.percentile(arr, 95))
                p99 = float(np.percentile(arr, 99))
                results.append(LengthResult(
                    length=length,
                    p50_ms=p50, p95_ms=p95, p99_ms=p99,
                    mean_ms=float(arr.mean()), max_ms=float(arr.max()),
                    passed=p99 < self.hard_limit_ms,
                ))
                logger.info(
                    "[ml] 延迟基准 len=%d: P99=%.3fms（passed=%s）",
                    length, p99, results[-1].passed,
                )
            overall = all(r.passed for r in results)
            return {
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "environment": {
                    "python": sys.version.split()[0],
                    "platform": platform.platform(),
                },
                "model_name": model_name,
                "train_rows": TRAIN_ROWS,
                "hard_limit_ms": self.hard_limit_ms,
                "target_ms": self.target_ms,
                "repeat": self.repeat,
                "lengths": [r.to_dict() for r in results],
                "overall_passed": overall,
                "note": (
                    f"口径：predict_realtime 全链路（compute_incremental+模型predict）"
                    f"耗时，每长度重复 {self.repeat} 次取百分位；"
                    f"硬阈值 P99<{self.hard_limit_ms}ms，初始目标 P99<{self.target_ms}ms；"
                    f"合成随机游走数据，独立临时 registry，不连实盘。"
                ),
            }
        finally:
            if own_registry:
                import shutil
                shutil.rmtree(Path(tmp_dir), ignore_errors=True)


__all__ = [
    "DEFAULT_LENGTHS",
    "DEFAULT_REPEAT",
    "DEFAULT_SEED",
    "TRAIN_ROWS",
    "HARD_LIMIT_MS",
    "TARGET_MS",
    "LengthResult",
    "InferenceBenchmark",
]
