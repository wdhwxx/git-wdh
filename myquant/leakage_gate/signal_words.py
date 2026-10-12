# 文件名：myquant/leakage_gate/signal_words.py
"""
leakage_gate/signal_words.py - R03/R07 共用"全样本接收者命名"信号词判定
=========================================================================
static_scan（R03 跨时间全样本拟合）与 cross_sectional（R07 横截面排名/分位）
共用同一套"接收者命名是否具全样本意图"的判定（panel/full/all_/entire/whole）。

本模块把该判定收敛为**单一实现**（P1-07⑥a / S-6：统一信号词 helper、R03/R07
同批同向处置），避免两处重复实现漂移。任何一侧要调整信号词，只需改这里。

分层：leakage_gate=L0。本模块只做纯命名判定，不 import strategies 包
（L0→L3 反向跨层会被 R-02 拦截），不持有运行数据、不预测未来（防未来函数）。
"""

from __future__ import annotations

from typing import Tuple

# R03/R07 全样本接收者命名信号词（panel/full/all_/entire/whole）
FULL_SAMPLE_RECEIVERS: Tuple[str, ...] = ("panel", "full", "all_", "entire", "whole")


def is_full_sample_receiver(name: str) -> bool:
    """接收者命名是否具全样本意图（R03/R07 共用信号词判定）。

    Args:
        name: 接收者变量/属性名（如 panel / all_factors / df_full）。

    Returns:
        bool: 命名小写后以任一信号词开头或包含任一信号词时为 True。
    """
    low = name.lower()
    return any(low.startswith(t) or t in low for t in FULL_SAMPLE_RECEIVERS)
