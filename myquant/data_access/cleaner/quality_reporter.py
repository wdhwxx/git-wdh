# 文件名：myquant/data_access/cleaner/quality_reporter.py
"""
quality_reporter.py - 数据质量报告生成 (折中档位2 v1)
======================================================
v1 实现: 简单函数输出 JSON 质量报告

功能:
  - 汇总清洗统计信息
  - 输出 JSON 报告到 outputs/ 目录
  - 报告包含: 标的、数据类型、时间、统计指标、脏数据概览

TODO(阶段9): 增强报告能力
  - HTML 可视化报告
  - 多标的对比报告
  - 历史趋势图
  - 报告入库 (database)
  - 数据质量评分
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from myquant.core.logger import get_logger
from myquant.utils.file_utils import ensure_dir, get_project_path, write_json

logger = get_logger("data_clean.quality_report")


def generate_quality_report(
    stats: Dict[str, Any],
    symbol: str = "unknown",
    data_type: str = "bar",
    output_dir: Optional[str] = None,
    bad_count: Optional[int] = None,
    extra_info: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    生成数据质量报告 (JSON格式)

    Parameters
    ----------
    stats : Dict
        清洗统计信息（来自 clean_bar_records / clean_tick_records 的返回值）
    symbol : str
        标的代码
    data_type : str
        数据类型: bar | tick
    output_dir : str | None
        报告输出目录，None 则使用默认 outputs/
    bad_count : int | None
        脏数据数量（如果 stats 中没有则从此参数取）
    extra_info : Dict | None
        额外信息（如数据源、配置版本等）

    Returns
    -------
    str | None : 报告文件路径，None 表示生成失败
    """
    if output_dir is None:
        output_dir = str(get_project_path("outputs"))

    ensure_dir(output_dir)

    # 组装报告
    report: Dict[str, Any] = {
        "report_type": "data_quality_report",
        "version": "1.0",
        "generated_at": datetime.now().isoformat(),
        "symbol": symbol,
        "data_type": data_type,
        "summary": {
            "total_records": stats.get("total", 0),
            "valid_records": stats.get("valid", 0),
            "bad_records": bad_count if bad_count is not None else stats.get("bad", 0),
            "valid_rate": 0.0,
            "removed_duplicates": stats.get("removed_duplicates", 0),
            "price_jumps": stats.get("price_jumps", 0),
            "volume_anomalies": stats.get("volume_anomalies", 0),
            "time_violations": stats.get("time_violations", 0),
            "duration_sec": stats.get("duration_sec", 0.0),
        },
        "quality_status": "unknown",
    }

    # 计算合格率
    total = report["summary"]["total_records"]
    valid = report["summary"]["valid_records"]
    if total > 0:
        report["summary"]["valid_rate"] = round(valid / total, 4)

    # 质量状态判定
    # #C11修复：excellent 不再强制 bad==0（真实行情完全无脏数据几乎不可能），
    # 改为 valid_rate >= 0.995（即脏数据占比≤0.5%）即可评为优秀。
    valid_rate = report["summary"]["valid_rate"]
    report["summary"]["bad_records"]
    if valid_rate >= 0.995:
        report["quality_status"] = "excellent"
    elif valid_rate >= 0.95:
        report["quality_status"] = "good"
    elif valid_rate >= 0.80:
        report["quality_status"] = "fair"
    else:
        report["quality_status"] = "poor"

    # 附加信息
    if extra_info:
        report["extra_info"] = extra_info

    # 写入文件
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"quality_report_{data_type}_{symbol}_{timestamp}.json"
    filepath = Path(output_dir) / filename

    try:
        write_json(str(filepath), report)
        logger.info(
            f"质量报告已生成: {filepath} "
            f"(合格率={report['summary']['valid_rate']:.1%}, 状态={report['quality_status']})"
        )
        return str(filepath)
    except Exception as e:
        logger.error(f"生成质量报告失败: {e}")
        return None
