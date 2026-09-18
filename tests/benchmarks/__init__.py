# 文件名：tests/benchmarks/__init__.py
"""
性能基准测试包
================
包含技术指标计算耗时、引擎吞吐量、下单延迟三类基准测试。

运行方式：
  独立运行：python tests/benchmarks/test_indicator_perf.py
  pytest收集：pytest tests/benchmarks/ -v
  全部运行：python tests/benchmarks/run_all_benchmarks.py

标记控制：
  pytest 默认收集所有测试；基准测试函数名以 test_ 开头。
  如需跳过基准：pytest tests/ --ignore=tests/benchmarks
"""
