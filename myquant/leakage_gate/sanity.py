# 文件名：myquant/leakage_gate/sanity.py
"""
leakage_gate/sanity.py - 阶段 2 模型层统计门禁（轨道 E，M2-01~06）
=================================================================
写代码/训练期拦截"模型层统计性泄漏"，逻辑全部下沉 leakage_gate（R-24 约束：
training_pipeline.py 行数 ≤ 1651，新增逻辑不落 ml 层）。

内容（v3.0 §6.6 / 施工任务清单 M2）：
  - M2-01 shuffle-label 探测：先跑基线实验定阈值，再挂训练流水线。
        打乱标签性能分布有基线；真实指标显著 > 阈值 → 拦截/告警。
  - M2-02 训练侧时间校验（纯函数，训练期接线）：特征最近时间 ≤ train_end_time
        且 > train_start_time；时间越界 → 训练侧拦截（raise 由接线方转 TrainingError）。
  - M2-03 特征-标签时间对齐校验（纯函数）：特征 available_time ≤ 标签 decision_time；
        对齐不通过 → 拦截。
  - M2-04 特征白名单扩充过 R11（gate check "feature_whitelist"）：features.py
        BAR_CN_COLUMN_MAP 任何新增列须过消费侧门禁——把 severity=阻断 列（如每股收益）
        加进白名单 → 出口报红（R11）。
  - M2-05/06 种子 sidecar（SeedSidecar）：monte_carlo / param_optimizer 随机种子
        记录在案；种子不一致 → 阻断。

防未来函数：本模块只分析"已发生"的实验结果/已给出时间与种子，不预测未来；
shuffle-label 基线只重排已发生标签，不引入未来数据。
"""

from __future__ import annotations

import ast
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from myquant.leakage_gate.contract import _load_ledger
from myquant.leakage_gate.gate import Violation, register_gate_check

logger = logging.getLogger("myquant.leakage_gate.sanity")

# 台账阻断档 severity（v3.0 §6.1）
_BLOCKED_SEVERITY: str = "阻断"

# features.py 路径（AST 读取 BAR_CN_COLUMN_MAP，非 import，分层合规）
_FEATURES_PATH: Path = Path(__file__).resolve().parents[2] / "myquant" / "ml" / "features.py"

# shuffle-label 显著阈值倍率（真实指标 > 打乱基线 mean + k*std → 告警）
_SHUFFLE_K_STD: float = 3.0
# 种子 sidecar 元信息键
_SEED_FILE_KEY: str = "_seed_sidecar_v1"


# ═══════════════════════════════════════════════
#  M2-01 shuffle-label 探测
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class ShuffleLabelBaseline:
    """打乱标签性能分布的基线（M2-01 验收：分布有基线 + 阈值）。

    Attributes:
        metric: 指标名（ic/accuracy/auc 等，由 scorer 返回值的语义决定）。
        n_perm: 打乱重数（基线实验次数）。
        values: 每次打乱后训练的指标序列。
        mean/std: 分布均值/标准差（打乱标签 ≈ 无信号噪声）。
        p95/p99: 高分位数（噪声上界）。
        threshold: 告警阈值 = mean + k*std（k 见 _SHUFFLE_K_STD）。
        seed: 打乱用随机种子（可复现基线）。
    """

    metric: str
    n_perm: int
    values: List[float]
    mean: float
    std: float
    p95: float
    p99: float
    threshold: float
    seed: int

    @property
    def noise_bound(self) -> float:
        """噪声上界（p99），真实指标远超其上界才判显著。"""
        return self.p99


def _percentile(values: Sequence[float], q: float) -> float:
    """对有序序列求分位（线性插值，纯 Python 实现）。"""
    if not values:
        return 0.0
    arr = sorted(float(v) for v in values)
    n = len(arr)
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return arr[lo]
    frac = pos - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def run_shuffle_label_baseline(
    scorer: Callable[[Any, Any], float],
    X: Any,
    y: Any,
    n_perm: int = 20,
    seed: int = 42,
    metric: str = "ic",
    rng: Optional[Any] = None,
) -> ShuffleLabelBaseline:
    """跑打乱标签基线实验，得到无信号时的性能分布（M2-01 先跑基线定阈值）。

    对 y 做 n_perm 次打乱，每次训练/评估取指标；打乱标签切断了 特征→标签 的
    真实映射，其性能 ≈ 噪声基线。阈值 = mean + k*std（显著 > 阈值 → 告警/拦截）。

    Args:
        scorer: 打乱标签后训练并返回指标的回调 scorer(X, y) -> float
            （由接线方注入；本模块不 import ml 层，分层合规）。
        X: 特征矩阵。
        y: 标签（将被原地复制并打乱）。
        n_perm: 打乱重数，>=5。
        seed: 打乱随机种子（可复现基线）。
        metric: 指标名（仅用于标签/记录）。
        rng: 可选随机数生成器；None 时用 np.random.default_rng(seed) 内部构造。

    Returns:
        ShuffleLabelBaseline: 基线分布（mean/std/p95/p99/threshold）。

    Raises:
        ValueError: n_perm < 5 或 scorer 不可调用。
    """
    if n_perm < 5:
        raise ValueError(f"n_perm 至少 5 以便基线稳定，收到: {n_perm}")
    if not callable(scorer):
        raise ValueError("scorer 必须是可调用对象（打乱标签后训练并返回指标）")
    if rng is None:
        import numpy as np
        rng = np.random.default_rng(seed)
    y_arr = _to_1d_numpy(y)
    values: List[float] = []
    for _ in range(int(n_perm)):
        shuffled = y_arr.copy()
        rng.shuffle(shuffled)
        try:
            values.append(float(scorer(X, shuffled)))
        except Exception as e:  # noqa: BLE001 - 打乱实验单项失败跳过（不吞主流程）
            logger.warning("shuffle-label 单次打乱实验失败，跳过: %s", e)
            continue
    if not values:
        raise RuntimeError("shuffle-label 基线实验全部失败，无法建立基线")
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(var)
    return ShuffleLabelBaseline(
        metric=metric,
        n_perm=len(values),
        values=values,
        mean=mean,
        std=std,
        p95=_percentile(values, 0.95),
        p99=_percentile(values, 0.99),
        threshold=mean + _SHUFFLE_K_STD * std,
        seed=seed,
    )


def check_shuffle_label(
    baseline: ShuffleLabelBaseline,
    real_metric: float,
    k_std: float = _SHUFFLE_K_STD,
) -> List[str]:
    """对照打乱标签基线判定真实指标（M2-01：显著 > 阈值 → 拦截/告警）。

    真实指标显著高于打乱标签噪声上界，说明模型在"标签已打乱"时仍无此高绩效，
    该高绩效非由真实可学习信号之外因素驱动（防未来：排除仅靠噪声偶然达成）。

    Args:
        baseline: 打乱标签基线（run_shuffle_label_baseline 产出）。
        real_metric: 真实（不打乱标签）模型的同一指标。
        k_std: 阈值倍率（默认 _SHUFFLE_K_STD=3）。

    Returns:
        List[str]: 违规描述；无显著异常为空列表（放行）。
    """
    if real_metric > baseline.noise_bound and real_metric > baseline.mean + k_std * baseline.std:
        return [
            f"shuffle-label 探测告警（RL-07）：真实指标 {real_metric:.4f} 显著高于打乱标签基线"
            f"噪声上界 p99={baseline.noise_bound:.4f} / 阈值={baseline.threshold:.4f}"
            f"（baseline metric={baseline.metric}, n={baseline.n_perm}, seed={baseline.seed}）——"
            f"该高绩效疑似依赖特征-标签异常关联，建议人工复核后放行或拦截"
        ]
    return []


# ═══════════════════════════════════════════════
#  M2-02 训练侧时间校验 / M2-03 特征-标签对齐（纯函数）
# ═══════════════════════════════════════════════

def _as_epoch(value: Any) -> float:
    """把时间值统一为 epoch 秒（数字原样、字符串按 ISO 解析），与 _ts_to_str 同口径。"""
    import pandas as pd

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(pd.Timestamp(value).timestamp())
        except Exception:  # noqa: BLE001
            raise ValueError(f"时间无法解析为 epoch 秒: {value!r}")
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return float(pd.Timestamp(value).timestamp())


def validate_train_temporal(
    timestamps: Sequence[Any],
    train_start_time: str,
    train_end_time: str,
) -> List[str]:
    """训练侧时间校验（M2-02）：特征最近时间 ≤ train_end_time 且 > train_start_time。

    由训练流水线在写入 train_end_time 时接线调用（推理侧守卫已有
    inference_service.py:327-347，本函数补训练侧对称守卫）。校验对象是
    "特征最近时间"（样本时点最大值，epoch 秒口径与 _ts_to_str 一致）。

    Args:
        timestamps: 训练样本决策时点序列（epoch 秒 float 或 ISO 字符串）。
        train_start_time: 训练起始（排他边界）。
        train_end_time: 训练截止（含边界，防未来）。

    Returns:
        List[str]: 时间越界/退化描述；无违规为空列表。
    """
    if not timestamps:
        return ["训练样本时间为空，无法校验（fail-closed）"]
    try:
        start_ts = _as_epoch(train_start_time)
        end_ts = _as_epoch(train_end_time)
    except ValueError as e:
        return [f"train_start/end 时间解析失败: {e}"]
    if end_ts <= start_ts:
        return [f"train_end_time {train_end_time} 未严格晚于 train_start_time {train_start_time}"]
    latest = max(_as_epoch(t) for t in timestamps)
    bad: List[str] = []
    if latest > end_ts:
        bad.append(
            f"特征最近时间 {latest} 晚于 train_end_time {train_end_time}（未来泄漏，防未来红线）")
    if latest <= start_ts:
        bad.append(
            f"特征最近时间 {latest} 未严格晚于 train_start_time {train_start_time}（训练窗口退化）")
    return bad


def validate_feature_label_alignment(
    feature_available_times: Sequence[Any],
    label_decision_times: Sequence[Any],
) -> List[str]:
    """特征-标签时间对齐校验（M2-03）：特征 available_time ≤ 标签 decision_time。

    Args:
        feature_available_times: 各样本特征可得时点序列（available_time）。
        label_decision_times: 各样本标签决策时点序列（decision_time）。

    Returns:
        List[str]: 对齐不通过描述；全部对齐为空列表。
    """
    import pandas as pd

    if len(feature_available_times) != len(label_decision_times):
        return [
            f"特征/标签样本数不一致（{len(feature_available_times)} vs "
            f"{len(label_decision_times)}），无法对齐校验"
        ]
    bad: List[str] = []
    for i, (ft, lt) in enumerate(zip(feature_available_times, label_decision_times)):
        try:
            f_ts = pd.Timestamp(ft)
            l_ts = pd.Timestamp(lt)
        except Exception:  # noqa: BLE001
            bad.append(f"样本 {i} 特征/标签时间不可解析: {ft!r}/{lt!r}")
            continue
        if f_ts > l_ts:
            bad.append(
                f"样本 {i} 特征 available_time={ft} 晚于标签 decision_time={lt}（特征用了"
                f"未来可得数据，防未来红线）"
            )
    return bad


# ═══════════════════════════════════════════════
#  M2-04 特征白名单扩充过 R11（gate check "feature_whitelist"）
# ═══════════════════════════════════════════════

def _read_feature_whitelist(source: str) -> List[str]:
    """从 features.py 源码提取 BAR_CN_COLUMN_MAP 的字符串值（AST 读取，非 import）。

    Args:
        source: features.py 源码文本。

    Returns:
        List[str]: 白名单列名（BAR_CN_COLUMN_MAP.values()）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        logger.error(f"features.py 源码解析失败: {e}")
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id == "BAR_CN_COLUMN_MAP"):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            cols: List[str] = []
            for key in node.value.values:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    cols.append(key.value)
            return cols
    return []


def check_feature_whitelist(strategy_id: str, decision_time: str) -> List[Violation]:
    """特征白名单门禁（register_gate_check("feature_whitelist", ...)，M2-04）。

    features.py `BAR_CN_COLUMN_MAP` 的任何新增列须过消费侧门禁：若白名单含
    台账 severity=阻断 的列（如每股收益，休眠穿越列最可能引爆点）→ R11 报红。
    只读 features.py 文件做 AST 提取（L0→ml 反向 import 会被 R-02 拦，故读文件）。

    Args:
        strategy_id: 策略契约主键（此处占位，实际校验对象为特征白名单）。
        decision_time: 决策时点（T日）。

    Returns:
        List[Violation]: R11 阻断违规（白名单含阻断列）；无违规为空列表。
    """
    ledger = _load_ledger()
    if ledger is None:
        return [Violation(
            gate="feature_whitelist", rule_id="R11", severity=_BLOCKED_SEVERITY,
            message="列级台账缺失，特征白名单无法校验（fail-closed）",
            strategy_id=strategy_id, decision_time=decision_time,
        )]
    blocked = _ledger_blocked(ledger)
    if not blocked:
        return []
    if not _FEATURES_PATH.is_file():
        return []  # features.py 不存在则无从校验（不作假阳性）
    try:
        source = _FEATURES_PATH.read_text(encoding="utf-8-sig")
    except OSError as e:
        logger.error(f"features.py 读取失败: {e}")
        return []
    whitelist = _read_feature_whitelist(source)
    hit = sorted(set(whitelist) & blocked)
    if not hit:
        return []
    return [
        Violation(
            gate="feature_whitelist", rule_id="R11", severity=_BLOCKED_SEVERITY,
            message=f"特征白名单 BAR_CN_COLUMN_MAP 新增列 {hit} 命中台账阻断档（R11）："
                    f"阻断列不得进入特征/回测/实盘链路（休眠穿越列最可能引爆点，M2-04）",
            strategy_id=strategy_id, decision_time=decision_time,
        )
        for _ in hit
    ]


def check_feature_whitelist_on_columns(
    feature_columns: Sequence[str],
    decision_time: str = "",
    strategy_id: str = "features",
) -> List[Violation]:
    """对给定特征列集合校验（供训练侧接线/反向测试用，M2-04）。

    Args:
        feature_columns: 特征列集合（如把 每股收益 加入白名单的模拟）。
        decision_time: 决策时点。
        strategy_id: 占位策略标识。

    Returns:
        List[Violation]: R11 阻断违规。
    """
    ledger = _load_ledger()
    if ledger is None:
        return [Violation(
            gate="feature_whitelist", rule_id="R11", severity=_BLOCKED_SEVERITY,
            message="列级台账缺失，特征白名单无法校验（fail-closed）",
            strategy_id=strategy_id, decision_time=decision_time,
        )]
    blocked = _ledger_blocked(ledger)
    hit = sorted(set(feature_columns) & blocked)
    if not hit:
        return []
    return [
        Violation(
            gate="feature_whitelist", rule_id="R11", severity=_BLOCKED_SEVERITY,
            message=f"特征列 {hit} 命中台账阻断档（R11）：阻断列不得进入特征/回测/实盘链路（M2-04）",
            strategy_id=strategy_id, decision_time=decision_time,
        )
    ]


def _ledger_blocked(ledger: Dict[str, Any]) -> set:
    """提取台账 severity=阻断 的列集合。"""
    columns = ledger.get("columns")
    if not isinstance(columns, dict):
        return set()
    return {
        c for c, m in columns.items()
        if isinstance(m, dict) and str(m.get("severity", "")).strip() == _BLOCKED_SEVERITY
    }


def check_training_sanity(
    train_timestamps: Sequence[Any],
    train_start_time: str,
    train_end_time: str,
) -> List[str]:
    """训练侧模型层门禁汇总（M2-02/03/04，供 training_pipeline run() 薄接线）。

    汇总 validate_train_temporal（特征时间 ≤ train_end_time 且 > train_start_time）、
    validate_feature_label_alignment（特征 available_time ≤ 标签 decision_time，
    训练期特征与标签共用同一决策时点，显式化该不变量）与特征白名单 R11
    （features.py BAR_CN_COLUMN_MAP 新增阻断列 → 拦截）。违规 → 返回描述列表。

    Args:
        train_timestamps: 训练样本决策时点序列。
        train_start_time: 训练起始（排他边界）。
        train_end_time: 训练截止（含边界，防未来）。

    Returns:
        List[str]: 违规描述；全部通过为空列表。
    """
    bad = validate_train_temporal(train_timestamps, train_start_time, train_end_time)
    bad += validate_feature_label_alignment(train_timestamps, train_timestamps)
    wl = check_feature_whitelist("training", train_end_time or "")
    if wl:
        bad.append(wl[0].message)
    return bad


# ═══════════════════════════════════════════════
#  M2-05/06 种子 sidecar（可复现 + 种子不一致阻断）
# ═══════════════════════════════════════════════

class SeedSidecarError(RuntimeError):
    """种子 sidecar 不一致：同一实验种类已记录不同随机种子 → 阻断。"""


class SeedSidecar:
    """随机种子 sidecar：记录在案 + 种子不一致阻断（M2-05/06）。

    JSON 侧车文件 {path} 映射 {实验种类: seed}。同一种类首次运行记录种子；
    后续运行若传入不同种子 → verify/check_or_record 抛 SeedSidecarError 阻断
    （保证结果可复现、种子留痕）。kind=None 时不启用（真随机/测试隔离）。

    Args:
        path: sidecar 文件路径（目录不存在时自动创建）。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path: Optional[Path] = Path(path) if path else None

    def load(self) -> Dict[str, Any]:
        """读取 sidecar 内容（文件不存在/损坏返回空 dict）。"""
        if self.path is None or not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"种子 sidecar 读取失败: {e}")
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        """写回 sidecar（原子写：先写临时文件再替换）。"""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def verify(self, kind: str, seed: Optional[int]) -> bool:
        """校验种子一致性：已记录同种类且种子不同 → False（不一致阻断信号）。

        Args:
            kind: 实验种类（monte_carlo / param_optimizer 等）。
            seed: 本次拟使用种子；None 表示真随机（不校验，返回 True）。

        Returns:
            bool: 一致或无记录/None 种子返回 True；已记录且不同返回 False。
        """
        if seed is None:
            return True
        data = self.load()
        recorded = data.get(kind)
        if recorded is None:
            return True  # 首次，尚未记录
        return int(recorded) == int(seed)

    def check_or_record(self, kind: str, seed: Optional[int]) -> None:
        """校验并记录（M2-05/06 接线用）：种子不一致 → 抛 SeedSidecarError 阻断。

        Args:
            kind: 实验种类。
            seed: 本次种子；None 表示真随机（跳过）。

        Raises:
            SeedSidecarError: 已记录同种类且种子不同（种子不一致阻断）。
        """
        if seed is None:
            return
        if not self.verify(kind, seed):
            recorded = self.load().get(kind)
            raise SeedSidecarError(
                f"种子 sidecar 不一致（{kind}）：已记录 seed={recorded}，本次传入 "
                f"seed={seed}；为保结果可复现与留痕，阻断本次运行（M2-05/06）"
            )
        data = self.load()
        data[kind] = int(seed)
        self._save(data)

    def recorded_seed(self, kind: str) -> Optional[int]:
        """查询某种类已记录种子（无则 None）。"""
        data = self.load()
        val = data.get(kind)
        return int(val) if val is not None else None


# ═══════════════════════════════════════════════
#  工具
# ═══════════════════════════════════════════════

def _to_1d_numpy(y: Any) -> Any:
    """把标签转成一维 numpy 数组（供打乱）。"""
    import numpy as np
    arr = np.asarray(y)
    if arr.ndim > 1:
        arr = arr.reshape(-1)
    return arr


def ensure_registered() -> None:
    """幂等注册特征白名单门禁（导入即注册；测试 reset_gate_checks 后可恢复）。"""
    try:
        register_gate_check("feature_whitelist", check_feature_whitelist)
    except ValueError:
        pass  # 已注册（幂等）


ensure_registered()
