# 文件名：myquant/utils/csv_utils.py
"""
csv_utils.py - CSV 中文表头导出工具
====================================
为"给人看的"结果/报告类 CSV 提供统一的中文表头映射导出，
避免各导出点重复编写映射逻辑，统一 utf-8-sig 编码（Excel 直接打开中文不乱码）。

设计边界（重要，勿违反）:
  - 仅用于结果/报告类 CSV（交易明细、净值曲线、批量汇总、参数优化结果），
    这些 CSV 无程序回读，中文化零逻辑破坏；
  - 数据类 CSV（行情、下载/清洗产物、指纹输入）属于程序契约，
    表头保持英文，严禁使用本工具映射。

Usage:
    from myquant.utils.csv_utils import CSV_HEADER_MAPS, export_csv_cn
    export_csv_cn(df, "outputs/trades.csv", header_map=CSV_HEADER_MAPS["trades"])
"""

import logging
from pathlib import Path
from typing import Dict, Optional, Union

import pandas as pd

logger = logging.getLogger("myquant.utils.csv_utils")

# ── 中文表头映射常量（禁魔法字符串） ──────────────────────────────────
# key 为导出场景名，value 为 {英文列名: 中文表头}
CSV_HEADER_MAPS: Dict[str, Dict[str, str]] = {
    # 交易明细（对应 models/backtest.py TradeRecord.to_dict 全部 15 字段；
    # 兼容 models/trade.py Trade.to_dict 的 order_id/notional/total_cost）
    "trades": {
        "trade_id": "成交ID",
        "order_id": "订单ID",
        "symbol": "标的代码",
        "side": "方向",
        "quantity": "数量",
        "price": "成交价",
        "commission": "手续费",
        "slippage": "滑点",
        "notional": "成交金额",
        "total_cost": "总成本",
        "timestamp": "时间戳",
        "strategy_id": "策略ID",
        "pnl": "盈亏",
        "pnl_pct": "盈亏比例",
        "holding_days": "持仓天数",
        "signal_source": "信号来源",
        "entry_reason": "入场原因",
        "exit_reason": "出场原因",
    },
    # 净值曲线（对应 models/backtest.py EquityPoint.to_dict 全部字段）
    "equity": {
        "timestamp": "时间戳",
        "date": "日期",
        "equity": "净值",
        "cash": "现金",
        "position_value": "持仓市值",
        "drawdown": "当前回撤",
        "benchmark_equity": "基准净值",
    },
    # 批量回测持仓市值序列（来源：净值曲线的 position_value 列）
    "positions": {
        "symbol": "标的代码",
        "date": "日期",
        "position_value": "持仓市值",
    },
    # 批量回测汇总（batch_summary，对应 batch_backtest_tab 导出列）
    "batch_summary": {
        "symbol": "标的代码",
        "status": "状态",
        "total_return": "总收益率",
        "sharpe_ratio": "夏普比率",
        "max_drawdown": "最大回撤",
        "trade_count": "交易次数",
        "equity_length": "净值长度",
        "mode": "引擎模式",
        "source": "数据来源",
        "error": "错误信息",
    },
    # 参数优化结果（param_optimizer.to_dataframe 静态列；param_* 动态列走前缀映射）
    "param_optimize": {
        "symbol": "标的代码",
        "status": "状态",
        "total_return": "总收益率",
        "sharpe_ratio": "夏普比率",
        "max_drawdown": "最大回撤",
        "trade_count": "交易次数",
        "error": "错误信息",
    },
}

# 动态列前缀映射（如 param_fast_period → 参数:fast_period）
PARAM_COLUMN_PREFIX: str = "param_"
PARAM_COLUMN_CN_PREFIX: str = "参数:"


def to_chinese_headers(
    df: pd.DataFrame,
    header_map: Dict[str, str],
    prefix_map: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """将 DataFrame 列名按映射替换为中文表头。

    只替换 header_map 中存在的列；未映射列保留原名；prefix_map 用于
    动态列前缀映射（如参数优化结果的 param_* 列）。

    Args:
        df: 待导出的数据表。
        header_map: 英文列名 → 中文表头映射。
        prefix_map: 列名前缀 → 中文前缀映射（默认 None，不启用）。

    Returns:
        列名已中文化的 DataFrame 副本（不修改入参）。

    Raises:
        ValueError: df 为空、header_map 为空，或映射后产生重复列名时抛出。
    """
    if df is None or len(df) == 0:
        raise ValueError("待导出 DataFrame 不能为空")
    if not header_map:
        raise ValueError("header_map 不能为空")

    renamed: Dict[str, str] = {}
    for col in df.columns:
        col_str = str(col)
        if col_str in header_map:
            renamed[col_str] = header_map[col_str]
        elif prefix_map:
            for prefix, cn_prefix in prefix_map.items():
                if col_str.startswith(prefix):
                    renamed[col_str] = f"{cn_prefix}{col_str[len(prefix):]}"
                    break

    result = df.rename(columns=renamed)
    if result.columns.duplicated().any():
        dup = sorted(
            {name for name, is_dup in zip(result.columns, result.columns.duplicated()) if is_dup}
        )
        raise ValueError(f"中文表头映射后产生重复列名: {dup}，请检查 header_map")
    return result


def export_csv_cn(
    df: pd.DataFrame,
    output_path: Union[str, Path],
    header_map: Dict[str, str],
    prefix_map: Optional[Dict[str, str]] = None,
    encoding: str = "utf-8-sig",
) -> Path:
    """以中文表头导出 CSV（默认 utf-8-sig，Excel 直接打开中文不乱码）。

    Args:
        df: 待导出数据。
        output_path: 输出路径（父目录自动创建）。
        header_map: 英文列名 → 中文表头映射。
        prefix_map: 列名前缀映射（默认 None）。
        encoding: 输出编码，默认 utf-8-sig。

    Returns:
        实际写入的 Path。

    Raises:
        ValueError: output_path 为空，或表头映射产生重复列名时抛出。
    """
    if not output_path:
        raise ValueError("output_path 不能为空")

    out = Path(output_path)
    renamed = to_chinese_headers(df, header_map, prefix_map)
    out.parent.mkdir(parents=True, exist_ok=True)
    renamed.to_csv(out, index=False, encoding=encoding)
    logger.info("CSV已导出(中文表头): %s (%d 行)", out, len(renamed))
    return out


if __name__ == "__main__":
    # 演示：构造示例表验证中文表头映射与导出
    _demo_df = pd.DataFrame(
        {
            "symbol": ["600343.SH", "603226.SH"],
            "total_return": [0.3656, 0.2268],
            "sharpe_ratio": [1.95, 1.26],
            "param_fast_period": [5, 10],
            "extra_col": ["保留原名", "保留原名"],
        }
    )
    _demo_out = Path(__file__).resolve().parent / "_demo_chinese_headers.csv"
    export_csv_cn(
        _demo_df,
        _demo_out,
        header_map=CSV_HEADER_MAPS["batch_summary"],
        prefix_map={PARAM_COLUMN_PREFIX: PARAM_COLUMN_CN_PREFIX},
    )
    _back = pd.read_csv(_demo_out, encoding="utf-8-sig")
    print("中文表头:", list(_back.columns))
    _demo_out.unlink(missing_ok=True)
