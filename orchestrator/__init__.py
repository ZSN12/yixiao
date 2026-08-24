# -*- coding: utf-8 -*-
"""调度编排层: 用 LangGraph StateGraph 把流水线建模为多智能体状态图。

包内容:
- language_graph_flow: 分析师 -> 匹配师 -> 推送员 的状态图编排(可独立运行);
  编译/调用失败自动降级为直接按顺序调用各模块, 保证 main.py 全流程
  不依赖 langgraph 可用性。
"""
