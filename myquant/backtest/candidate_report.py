# -*- coding: utf-8 -*-
# 文件名：myquant/backtest/candidate_report.py
"""
候选池交叉验证报告生成器
========================
基于 build_candidate_pool 返回的候选池 DataFrame（英文列，按综合夏普降序），
生成 HTML / PDF 汇总文档，与 CSV（双强候选池_<时间戳>.csv）同目录同时间戳，
构成"候选交叉池"一套文档：
  - HTML：统计卡 + 综合夏普分布直方图 + Top N 榜 + 全量候选池绩效表
  - PDF：统计表 + 综合夏普分布直方图 + Top N 榜 + 全量候选池表（fpdf2 离线）

设计原则（docs/py文件代码规则.md）：
  - 全量类型注解 + Google docstring + 防御式校验 + logging
  - 报告只展示已实现的候选池交叉验证结果（防未来函数铁律）
  - 候选池 DataFrame 列与 OUTPUT_COLUMNS 一致；字段缺失时防御式降级
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from myquant.backtest.pdf_report import _ReportPdf, PdfReportGenerator
from myquant.core.logger import get_logger

logger = get_logger(__name__)

# ── 常量（模块顶部，禁止魔法数字）──────────────────────────────────────
DEFAULT_TITLE: str = "鲲鹏量化交易系统 - 双策略交叉验证候选池"
HIST_BINS: int = 30           # 综合夏普分布直方图分箱数

# A4 页面与边距（mm，与 pdf_report.py 对齐）
_PAGE_W: float = 210.0
_PAGE_H: float = 297.0
_MARGIN: float = 10.0
_ROW_H: float = 7.0
_PLOT_H: float = 52.0
_PLOT_W: float = _PAGE_W - 2 * _MARGIN   # 190
_TABLE_BOTTOM_MARGIN: float = 30.0       # 表分页底线余量

# 候选池英文字段 → 中文表头（与 candidate_pool.CSV_HEADERS_ZH 一致）
POOL_HEADERS_CN: Dict[str, str] = {
    "symbol": "股票代码",
    "rsrs_ret": "rsrs收益率",
    "rsrs_sharpe": "rsrs夏普比率",
    "rsrs_max_drawdown": "rsrs最大回撤",
    "rsrs_trades": "rsrs交易次数",
    "ma_cross_ret": "ma_cross收益率",
    "ma_cross_sharpe": "ma_cross夏普比率",
    "ma_cross_max_drawdown": "ma_cross最大回撤",
    "ma_cross_trades": "ma_cross交易次数",
    "avg_sharpe": "综合夏普比率",
    "is_top30": "是否Top30",
}

# PDF 精简表列（A4 放不下 11 列，取核心绩效列）
_PDF_TABLE_HEADERS: List[str] = ["#", "标的", "综合夏普", "rsrs收益", "rsrs夏普", "ma收益", "ma夏普", "是否Top"]
_PDF_TABLE_WIDTHS: List[float] = [10, 34, 24, 26, 24, 26, 24, 22]
_PDF_TABLE_ALIGNS: List[str] = ["C", "L", "R", "R", "R", "R", "R", "C"]


@dataclass(frozen=True)
class CandidateReportMeta:
    """候选池报告元信息（过滤条件与 Top 口径）。

    Attributes:
        strategy_pair: 策略组合描述，如 "rsrs × ma_cross"。
        min_sharpe: 单策略夏普下限。
        min_trades: 单策略交易数下限。
        max_drawdown: 单策略最大回撤上限（0.10 = 10%）。
        top_n: 综合夏普降序标记数量。
    """

    strategy_pair: str
    min_sharpe: float
    min_trades: int
    max_drawdown: float
    top_n: int


def _to_float(value: Any) -> Optional[float]:
    """把值安全转换为 float；None/非法返回 None（防御式）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class CandidatePoolReportGenerator:
    """候选池交叉验证报告生成器。

    输入为 build_candidate_pool 返回的 DataFrame（英文列）：
      symbol/rsrs_ret/rsrs_sharpe/rsrs_max_drawdown/rsrs_trades/
      ma_cross_ret/ma_cross_sharpe/ma_cross_max_drawdown/ma_cross_trades/
      avg_sharpe/is_top30（按 avg_sharpe 降序，Top N 已标记）。
    """

    def __init__(self, title: str = DEFAULT_TITLE) -> None:
        """初始化候选池报告生成器。

        Args:
            title: 报告标题。
        """
        self.title = title
        logger.info("候选池报告生成器初始化: title=%s", title)

    # ─────────────────────────────────────────────
    # 主入口
    # ─────────────────────────────────────────────

    def generate_full_report(
        self,
        pool: pd.DataFrame,
        meta: CandidateReportMeta,
        csv_path: Path,
    ) -> Dict[str, str]:
        """一次生成 HTML + PDF（CSV 已由 build_candidate_pool 导出）。

        HTML/PDF 与 CSV 同目录同文件前缀，构成一套文档。

        Args:
            pool: 候选池 DataFrame（英文列）。
            meta: 报告元信息。
            csv_path: 已导出的 CSV 路径（用于派生 HTML/PDF 文件名）。

        Returns:
            {"html": HTML 路径, "pdf": PDF 路径}。

        Raises:
            ValueError: pool 为空时抛出。
        """
        if pool is None or pool.empty:
            raise ValueError("候选池为空，无法生成报告")
        csv_path = Path(csv_path)
        stem = csv_path.stem  # 如 双强候选池_20260912_204815
        html_path = self.generate_html(
            pool, meta, csv_path.parent / f"{stem}.html",
        )
        pdf_path = self.generate_pdf(
            pool, meta, csv_path.parent / f"{stem}.pdf",
        )
        logger.info("候选池报告已生成: html=%s, pdf=%s", html_path, pdf_path)
        return {"html": str(html_path), "pdf": str(pdf_path)}

    # ─────────────────────────────────────────────
    # 数据统计（HTML/PDF 共用）
    # ─────────────────────────────────────────────

    def _compute_stats(
        self, pool: pd.DataFrame,
    ) -> Tuple[float, float, float, float, float, float, float]:
        """从候选池 DataFrame 计算统计指标。

        Args:
            pool: 候选池 DataFrame。

        Returns:
            (平均综合夏普, 平均rsrs收益, 平均ma收益, 平均rsrs夏普,
             平均ma夏普, 双策略正收益占比, Top N 标记数)。
        """
        avg_sharpe = float(pool["avg_sharpe"].mean()) if "avg_sharpe" in pool else 0.0
        avg_rsrs_ret = float(pool["rsrs_ret"].mean()) if "rsrs_ret" in pool else 0.0
        avg_ma_ret = float(pool["ma_cross_ret"].mean()) if "ma_cross_ret" in pool else 0.0
        avg_rsrs_sharpe = float(pool["rsrs_sharpe"].mean()) if "rsrs_sharpe" in pool else 0.0
        avg_ma_sharpe = float(pool["ma_cross_sharpe"].mean()) if "ma_cross_sharpe" in pool else 0.0
        both_positive = (
            float(((pool["rsrs_ret"] > 0) & (pool["ma_cross_ret"] > 0)).mean())
            if "rsrs_ret" in pool and "ma_cross_ret" in pool else 0.0
        )
        top_count = int(pool["is_top30"].sum()) if "is_top30" in pool else 0
        return (
            avg_sharpe, avg_rsrs_ret, avg_ma_ret,
            avg_rsrs_sharpe, avg_ma_sharpe, both_positive, top_count,
        )

    @staticmethod
    def _histogram_data(
        values: Sequence[float], bins: int = HIST_BINS,
    ) -> Tuple[List[str], List[int]]:
        """数值数组 → 直方图数据（标签=区间中点，计数）。

        Args:
            values: 数值序列。
            bins: 分箱数。

        Returns:
            (标签列表, 计数列表)；空输入返回两个空列表。
        """
        arr = np.asarray(values, dtype=float)
        if len(arr) == 0:
            return [], []
        counts, edges = np.histogram(arr, bins=bins)
        labels = [f"{(edges[i] + edges[i + 1]) / 2:.2f}" for i in range(len(counts))]
        return labels, [int(c) for c in counts]

    # ─────────────────────────────────────────────
    # HTML 报告
    # ─────────────────────────────────────────────

    def generate_html(
        self,
        pool: pd.DataFrame,
        meta: CandidateReportMeta,
        output_path: Path,
    ) -> str:
        """生成 HTML 候选池报告（统计卡 + 分布图 + Top N 榜 + 全量表）。

        Args:
            pool: 候选池 DataFrame。
            meta: 报告元信息。
            output_path: 输出路径。

        Returns:
            str: 报告文件路径。
        """
        (
            avg_sharpe, avg_rsrs_ret, avg_ma_ret,
            avg_rsrs_sharpe, avg_ma_sharpe, both_positive, top_count,
        ) = self._compute_stats(pool)
        sharpe_labels, sharpe_counts = self._histogram_data(pool["avg_sharpe"].tolist())

        # Top N 表（按综合夏普降序，取前 top_n 行）
        top_rows_html = ""
        for i, (_, row) in enumerate(pool.head(meta.top_n).iterrows(), start=1):
            top_rows_html += self._row_html(i, row)

        # 全量候选池表
        all_rows_html = ""
        for i, (_, row) in enumerate(pool.iterrows(), start=1):
            all_rows_html += self._row_html(i, row)

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{self.title}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f7fa; color: #333; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 12px; margin-bottom: 20px; }}
        .header h1 {{ font-size: 24px; margin-bottom: 10px; }}
        .header .meta {{ opacity: 0.9; font-size: 14px; }}
        .card {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .card h2 {{ font-size: 18px; margin-bottom: 16px; color: #2c3e50; border-left: 4px solid #667eea; padding-left: 12px; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; }}
        .metric-item {{ background: #f8f9fa; padding: 16px; border-radius: 8px; }}
        .metric-item .label {{ font-size: 12px; color: #7f8c8d; margin-bottom: 4px; }}
        .metric-item .value {{ font-size: 20px; font-weight: 600; color: #2c3e50; }}
        .metric-item .value.positive {{ color: #27ae60; }}
        .metric-item .value.negative {{ color: #e74c3c; }}
        .chart-container {{ position: relative; height: 300px; margin-bottom: 20px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
        th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f8f9fa; font-weight: 600; color: #7f8c8d; }}
        tr:hover {{ background: #f8f9fa; }}
        .footer {{ text-align: center; color: #95a5a6; font-size: 12px; padding: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{self.title}</h1>
            <div class="meta">
                策略组合: {meta.strategy_pair} |
                过滤: 夏普≥{meta.min_sharpe} | 交易≥{meta.min_trades} | 回撤≤{meta.max_drawdown:.0%} |
                Top {meta.top_n}
            </div>
        </div>

        <div class="card">
            <h2>候选池统计（共 {len(pool)} 只双强标的）</h2>
            <div class="metrics-grid">
                <div class="metric-item"><div class="label">候选池总数</div><div class="value">{len(pool)}</div></div>
                <div class="metric-item"><div class="label">Top {meta.top_n} 标记</div><div class="value">{top_count}</div></div>
                <div class="metric-item"><div class="label">平均综合夏普</div><div class="value">{avg_sharpe:.4f}</div></div>
                <div class="metric-item"><div class="label">平均 rsrs 收益</div><div class="value {'positive' if avg_rsrs_ret >= 0 else 'negative'}">{avg_rsrs_ret:.2%}</div></div>
                <div class="metric-item"><div class="label">平均 ma_cross 收益</div><div class="value {'positive' if avg_ma_ret >= 0 else 'negative'}">{avg_ma_ret:.2%}</div></div>
                <div class="metric-item"><div class="label">双策略均正收益占比</div><div class="value">{both_positive:.2%}</div></div>
                <div class="metric-item"><div class="label">平均 rsrs 夏普</div><div class="value">{avg_rsrs_sharpe:.4f}</div></div>
                <div class="metric-item"><div class="label">平均 ma_cross 夏普</div><div class="value">{avg_ma_sharpe:.4f}</div></div>
            </div>
        </div>

        <div class="card">
            <h2>综合夏普分布</h2>
            <div class="chart-container"><canvas id="sharpeChart"></canvas></div>
        </div>

        <div class="card">
            <h2>Top {min(meta.top_n, len(pool))}（按综合夏普降序）</h2>
            <table>
                <thead><tr><th>#</th><th>股票代码</th><th>rsrs收益率</th><th>rsrs夏普</th><th>rsrs回撤</th><th>rsrs交易</th><th>ma收益率</th><th>ma夏普</th><th>ma回撤</th><th>ma交易</th><th>综合夏普</th><th>是否Top{meta.top_n}</th></tr></thead>
                <tbody>{top_rows_html}</tbody>
            </table>
        </div>

        <div class="card">
            <h2>全量候选池（共 {len(pool)} 只）</h2>
            <table>
                <thead><tr><th>#</th><th>股票代码</th><th>rsrs收益率</th><th>rsrs夏普</th><th>rsrs回撤</th><th>rsrs交易</th><th>ma收益率</th><th>ma夏普</th><th>ma回撤</th><th>ma交易</th><th>综合夏普</th><th>是否Top{meta.top_n}</th></tr></thead>
                <tbody>{all_rows_html}</tbody>
            </table>
        </div>

        <div class="footer">
            生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 鲲鹏量化交易系统 Pythonwdhmm
        </div>
    </div>

    <script>
        const sharpeLabels = {json.dumps(sharpe_labels, ensure_ascii=False)};
        const sharpeCounts = {json.dumps(sharpe_counts)};

        new Chart(document.getElementById('sharpeChart'), {{
            type: 'bar',
            data: {{ labels: sharpeLabels, datasets: [{{
                label: '标的数', data: sharpeCounts,
                backgroundColor: 'rgba(102, 126, 234, 0.6)', borderColor: '#667eea', borderWidth: 1
            }}] }},
            options: {{ responsive: true, maintainAspectRatio: false,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{ x: {{ ticks: {{ maxRotation: 45 }}, title: {{ display: true, text: '综合夏普' }} }},
                          y: {{ title: {{ display: true, text: '标的数' }} }} }} }}
        }});
    </script>
</body>
</html>"""
        os.makedirs(os.path.dirname(str(output_path)) or ".", exist_ok=True)
        Path(output_path).write_text(html, encoding="utf-8")
        logger.info("候选池 HTML 报告已生成: %s", output_path)
        return str(output_path)

    @staticmethod
    def _row_html(index: int, row: pd.Series) -> str:
        """单行候选池 → HTML 表格行。"""
        rsrs_ret = _to_float(row.get("rsrs_ret"))
        ma_ret = _to_float(row.get("ma_cross_ret"))
        avg_sharpe = _to_float(row.get("avg_sharpe"))
        is_top = bool(row.get("is_top30"))
        ret_cls = "positive" if (rsrs_ret or 0) >= 0 else "negative"
        return f"""
            <tr>
                <td>{index}</td>
                <td>{row.get('symbol', '')}</td>
                <td class="{ret_cls}">{rsrs_ret:.2%}</td>
                <td>{_to_float(row.get('rsrs_sharpe')):.4f}</td>
                <td>{_to_float(row.get('rsrs_max_drawdown')):.2%}</td>
                <td>{row.get('rsrs_trades', '-')}</td>
                <td>{ma_ret:.2%}</td>
                <td>{_to_float(row.get('ma_cross_sharpe')):.4f}</td>
                <td>{_to_float(row.get('ma_cross_max_drawdown')):.2%}</td>
                <td>{row.get('ma_cross_trades', '-')}</td>
                <td>{avg_sharpe:.4f}</td>
                <td>{"是" if is_top else "否"}</td>
            </tr>"""

    # ─────────────────────────────────────────────
    # PDF 报告
    # ─────────────────────────────────────────────

    def generate_pdf(
        self,
        pool: pd.DataFrame,
        meta: CandidateReportMeta,
        output_path: Path,
    ) -> str:
        """生成 PDF 候选池报告（fpdf2 离线渲染）。

        Args:
            pool: 候选池 DataFrame。
            meta: 报告元信息。
            output_path: 输出路径。

        Returns:
            str: 报告文件路径。
        """
        (
            avg_sharpe, avg_rsrs_ret, avg_ma_ret,
            avg_rsrs_sharpe, avg_ma_sharpe, both_positive, top_count,
        ) = self._compute_stats(pool)

        pdf = _ReportPdf(self.title)
        if not PdfReportGenerator._load_font(pdf):
            logger.warning("未找到中文字体，PDF 中文可能无法正常显示")
        self._render_pdf_title(pdf, meta, len(pool), top_count)
        self._render_pdf_stats(
            pdf, avg_sharpe, avg_rsrs_ret, avg_ma_ret,
            avg_rsrs_sharpe, avg_ma_sharpe, both_positive,
        )
        self._render_pdf_histogram(pdf, pool["avg_sharpe"].tolist(), "综合夏普分布")
        self._render_pdf_table(
            pdf, f"Top {min(meta.top_n, len(pool))}（按综合夏普降序）",
            pool.head(meta.top_n),
        )
        self._render_pdf_table(
            pdf, f"全量候选池（共 {len(pool)} 只）", pool,
        )
        os.makedirs(os.path.dirname(str(output_path)) or ".", exist_ok=True)
        pdf.output(str(output_path))
        logger.info("候选池 PDF 报告已生成: %s", output_path)
        return str(output_path)

    def _render_pdf_title(
        self, pdf: _ReportPdf, meta: CandidateReportMeta,
        pool_size: int, top_count: int,
    ) -> None:
        """PDF 标题区与元信息。"""
        pdf.add_page()
        pdf.set_font("cjk", "", 16)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(102, 126, 234)
        pdf.cell(0, 14, self.title, border=0, fill=True, align="C")
        pdf.ln(18)
        meta_rows: List[Tuple[str, str]] = [
            ("策略组合", meta.strategy_pair),
            ("过滤条件", f"夏普≥{meta.min_sharpe} | 交易≥{meta.min_trades} | 回撤≤{meta.max_drawdown:.0%}"),
            ("Top N", str(meta.top_n)),
            ("候选池总数", str(pool_size)),
            ("Top 标记数", str(top_count)),
        ]
        pdf.set_font("cjk", "", 9)
        for label, value in meta_rows:
            pdf.set_text_color(140, 140, 140)
            pdf.cell(28, _ROW_H, label)
            pdf.set_text_color(40, 40, 40)
            pdf.cell(0, _ROW_H, value)
            pdf.ln()
        pdf.ln(4)

    def _render_pdf_stats(
        self, pdf: _ReportPdf,
        avg_sharpe: float, avg_rsrs_ret: float, avg_ma_ret: float,
        avg_rsrs_sharpe: float, avg_ma_sharpe: float, both_positive: float,
    ) -> None:
        """PDF 候选池统计表。"""
        self._pdf_section_title(pdf, "候选池统计")
        rows: List[Tuple[str, str]] = [
            ("平均综合夏普", f"{avg_sharpe:.4f}"),
            ("平均 rsrs 收益", f"{avg_rsrs_ret:.2%}"),
            ("平均 ma_cross 收益", f"{avg_ma_ret:.2%}"),
            ("双策略均正收益占比", f"{both_positive:.2%}"),
            ("平均 rsrs 夏普", f"{avg_rsrs_sharpe:.4f}"),
            ("平均 ma_cross 夏普", f"{avg_ma_sharpe:.4f}"),
        ]
        self._pdf_key_value_table(pdf, rows)

    def _render_pdf_histogram(
        self, pdf: _ReportPdf, values: Sequence[float], title: str,
    ) -> None:
        """PDF 直方图（fpdf2 矩形柱）。"""
        self._pdf_section_title(pdf, f"{title}（{len(values)} 只）")
        arr = np.asarray(values, dtype=float)
        if len(arr) == 0:
            pdf.set_font("cjk", "", 9)
            pdf.cell(0, _ROW_H, "无有效数据，无法绘制")
            pdf.ln(6)
            return
        counts, edges = np.histogram(arr, bins=HIST_BINS)
        if counts.max() == 0:
            return
        if pdf.get_y() + _PLOT_H + 10 > _PAGE_H - 15:
            pdf.add_page()
        x0, y0 = _MARGIN, pdf.get_y()
        pdf.set_draw_color(200, 200, 200)
        pdf.rect(x0, y0, _PLOT_W, _PLOT_H)
        bar_w = _PLOT_W / len(counts)
        pdf.set_fill_color(102, 126, 234)
        for i, c in enumerate(counts):
            bh = (c / counts.max()) * (_PLOT_H - 8)
            pdf.rect(x0 + i * bar_w, y0 + _PLOT_H - 4 - bh, bar_w * 0.8, bh, style="F")
        pdf.set_font("cjk", "", 7)
        pdf.set_text_color(150, 150, 150)
        pdf.set_xy(x0, y0 + _PLOT_H - 4)
        pdf.cell(60, 4, f"min {edges[0]:.2f}", align="L")
        pdf.set_xy(x0 + _PLOT_W - 60, y0 + _PLOT_H - 4)
        pdf.cell(60, 4, f"max {edges[-1]:.2f}", align="R")
        pdf.set_y(y0 + _PLOT_H + 10)

    def _render_pdf_table(
        self, pdf: _ReportPdf, title: str, data: pd.DataFrame,
    ) -> None:
        """PDF 绩效表（精简列，自动分页，表头随页重复）。"""
        self._pdf_section_title(pdf, title)

        def _header_row() -> None:
            pdf.set_font("cjk", "", 8)
            pdf.set_fill_color(102, 126, 234)
            pdf.set_text_color(255, 255, 255)
            for i, h in enumerate(_PDF_TABLE_HEADERS):
                pdf.cell(_PDF_TABLE_WIDTHS[i], 6, h, border=1, fill=True, align="C")
            pdf.ln()
            pdf.set_text_color(40, 40, 40)

        _header_row()
        pdf.set_font("cjk", "", 8)
        for i, (_, row) in enumerate(data.iterrows(), start=1):
            if pdf.get_y() > _PAGE_H - _TABLE_BOTTOM_MARGIN:
                pdf.add_page()
                _header_row()
                pdf.set_font("cjk", "", 8)
            avg_sharpe = _to_float(row.get("avg_sharpe"))
            vals = [
                str(i),
                str(row.get("symbol", "")),
                f"{avg_sharpe:.4f}" if avg_sharpe is not None else "-",
                self._pct(row.get("rsrs_ret")),
                self._num(row.get("rsrs_sharpe")),
                self._pct(row.get("ma_cross_ret")),
                self._num(row.get("ma_cross_sharpe")),
                "是" if row.get("is_top30") else "否",
            ]
            pdf.set_text_color(40, 40, 40)
            for j, v in enumerate(vals):
                pdf.cell(_PDF_TABLE_WIDTHS[j], 6, v, border=1, align=_PDF_TABLE_ALIGNS[j])
            pdf.ln()

    @staticmethod
    def _pct(value: Any) -> str:
        """浮点值 → 百分比字符串；非法返回 '-'。"""
        f = _to_float(value)
        return f"{f:.2%}" if f is not None else "-"

    @staticmethod
    def _num(value: Any) -> str:
        """浮点值 → 4 位小数；非法返回 '-'。"""
        f = _to_float(value)
        return f"{f:.4f}" if f is not None else "-"

    # ─────────────────────────────────────────────
    # PDF 绘制工具
    # ─────────────────────────────────────────────

    def _pdf_section_title(self, pdf: _ReportPdf, text: str) -> None:
        """PDF 区块小标题（触底自动分页）。"""
        if pdf.get_y() > _PAGE_H - 40:
            pdf.add_page()
        pdf.set_font("cjk", "", 12)
        pdf.set_text_color(44, 62, 80)
        pdf.set_fill_color(102, 126, 234)
        pdf.cell(2, 8, "", fill=True)
        pdf.cell(4, 8, "")
        pdf.cell(0, 8, text)
        pdf.ln(12)

    @staticmethod
    def _pdf_key_value_table(
        pdf: _ReportPdf, rows: Sequence[Tuple[str, str]],
    ) -> None:
        """PDF 两列键值表（交替底色）。"""
        pdf.set_font("cjk", "", 9)
        for i, (k, v) in enumerate(rows):
            pdf.set_fill_color(248, 249, 250) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(52, _ROW_H, k, border=1, fill=True, align="L")
            pdf.set_text_color(40, 40, 40)
            pdf.cell(138, _ROW_H, v, border=1, fill=True, align="L")
            pdf.ln()
        pdf.ln(2)
