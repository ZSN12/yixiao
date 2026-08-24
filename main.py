# -*- coding: utf-8 -*-
"""销售线索智能分析与分发助手 —— 项目门面(main.py)。

两种运行模式:
1. 一次性跑通:  python main.py --run-once
   -> 加载数据 → LangGraph 多智能体流水线(分析师/匹配师/推送员)
      → 打印流水线摘要(summary)。
2. 每日定时:    python main.py
   -> APScheduler BlockingScheduler + CronTrigger, 按 settings.daily_run_time
      (格式 "HH:MM") 每天定点执行同一流水线。

健壮性设计(任务规定):
- 流水线内部按环节拆段(加载→分析→分配→推送), 每段独立 try/except:
  单客户失败跳过不中断(模块内部已兜底), 推送失败记日志(数据已落库可重推);
- 最外层再包一层兜底 try/except, 输出错误摘要而非堆栈崩溃;
- --skip-push 跳过推送环节; --verbose 打开调试日志。
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, List, Optional

# 项目根目录(本文件位于 <项目根>/main.py)加入 sys.path, 保证任何 cwd 下可运行
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from modules import data_loader
from orchestrator import language_graph_flow as lgf

logger = logging.getLogger("main")


# ============================================================
# 流水线主体(按环节拆段, 每段独立兜底)
# ============================================================


def run_pipeline(args: argparse.Namespace) -> Dict[str, Any]:
    """执行一次完整流水线(加载→分析→分配→推送), 返回最终状态 dict。

    每一环节独立 try/except: 单环节失败不中断后续环节;
    最外层再包一层兜底, 保证任何异常都以错误摘要形式返回而非崩溃。

    Args:
        args: argparse 解析结果(含 skip_push / verbose)。

    Returns:
        dict: 最终状态(与 orchestrator 输出的状态同构, 含 summary)。
    """
    errors: List[str] = []

    # ---- 环节 0: 数据加载(独立兜底) ----
    try:
        customers, records, sales = data_loader.load_all()
        chat_map = data_loader.build_chat_map(records)
        experiences = data_loader.load_sales_experiences()
        data_loader.init_db()   # 初始化 SQLite(分析历史落库; 失败只记日志)
        logger.info("数据加载完成: 客户 %d / 会话 %d / 销售 %d / 经验 %d",
                    len(customers), len(records), len(sales), len(experiences))
    except Exception as exc:  # noqa: BLE001 —— 数据缺失时给出中文错误摘要
        logger.error("数据加载失败: %s", exc)
        print(f"[错误摘要] 数据加载失败: {exc}\n"
              f"请确认项目 data/ 目录下已放置 mock 数据文件。")
        return {"summary": {
            "customer_count": 0, "analyzed_count": 0,
            "intention_stats": {"高": 0, "中": 0, "低": 0},
            "churn_stats": {"高": 0, "中": 0, "低": 0},
            "assignment_count": 0, "needs_human_count": 0,
            "push_ok": False, "push_hint": "数据加载失败, 流水线终止",
            "errors": [f"data_loading: {exc}"],
        }, "errors": [f"data_loading: {exc}"], "meta": {"engine": "failed"}}

    # ---- 环节 1+2+3: 多智能体流水线(分析师→匹配师→推送员) ----
    # 内部已按"每环节独立 try/except + langgraph 降级顺序直调"兜底,
    # 任何异常都以状态形式返回, 不会向上抛出。
    try:
        state = lgf.run_pipeline_graph(
            customers=customers,
            chat_map=chat_map,
            sales=sales,
            experiences=experiences,
            skip_push=args.skip_push,
        )
        errors = list(state.get("errors") or [])
    except Exception as exc:  # noqa: BLE001 —— 最外层兜底
        logger.error("流水线执行异常: %s", exc)
        errors.append(f"pipeline: {exc}")
        state = {
            "summary": {
                "customer_count": len(customers), "analyzed_count": 0,
                "intention_stats": {"高": 0, "中": 0, "低": 0},
                "churn_stats": {"高": 0, "中": 0, "低": 0},
                "assignment_count": 0, "needs_human_count": 0,
                "push_ok": False, "push_hint": f"流水线异常: {exc}",
                "errors": errors,
            },
            "errors": errors,
            "meta": {"engine": "failed"},
        }

    # ---- 环节 4: 推送状态兜底说明 ----
    # 推送已在流水线内部的推送员节点按 skip_push 处理(真正跳过);
    # 这里只做防御性兜底, 保证 summary 中始终有 push_hint。
    if args.skip_push:
        summary = state.setdefault("summary", {})
        summary["push_ok"] = False
        summary["push_hint"] = "--skip-push 跳过推送(数据已落库可稍后重推)"

    return state


# ============================================================
# 定时调度(APScheduler)
# ============================================================


def parse_daily_run_time(raw: Optional[str]) -> tuple:
    """解析 settings.daily_run_time("HH:MM")为 (hour, minute)。

    Args:
        raw: 原始配置字符串, 如 "09:00" / "8:30" / "08:30:00"(容错)。

    Returns:
        tuple[int, int]: (hour, minute)。

    Raises:
        ValueError: 格式非法时抛出(由调度入口兜底为默认值并告警)。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("daily_run_time 为空")
    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError(f"daily_run_time 格式非法: {text!r} (应为 HH:MM)")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"daily_run_time 越界: {text!r} (hour 0-23, minute 0-59)")
    return hour, minute


def start_scheduler(args: argparse.Namespace) -> None:
    """进入 APScheduler 定时模式: 每天按 settings.daily_run_time 执行流水线。

    Args:
        args: argparse 解析结果(透传给定时任务)。

    Notes:
        BlockingScheduler 阻塞主线程, 适合作为常驻进程/容器入口;
        可用 Ctrl+C 退出, 退出前打印友好提示。
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    # 解析每日运行时间; 非法时告警并回落默认 08:30, 保证调度器总能启动
    try:
        hour, minute = parse_daily_run_time(settings.daily_run_time)
    except ValueError as exc:
        logger.warning("%s; 回落默认 08:30", exc)
        hour, minute = 8, 30

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")

    def _scheduled_job() -> None:
        """定时任务: 跑一次流水线并打印摘要(含错误摘要兜底)。"""
        logger.info("定时任务触发: 开始执行销售线索流水线")
        try:
            state = run_pipeline(args)
            print(lgf.build_summary_text(state))
        except Exception as exc:  # noqa: BLE001 —— 最外层兜底, 输出错误摘要
            logger.error("定时任务执行异常: %s", exc)
            print(f"[错误摘要] 定时任务执行异常: {exc}")

    scheduler.add_job(
        _scheduled_job,
        CronTrigger(hour=hour, minute=minute),
        id="daily_sales_pipeline",
        name="每日销售线索智能流水线",
        misfire_grace_time=3600,   # 错过触发后 1 小时内补跑
        coalesce=True,             # 错过多次只补跑一次
    )
    print(f"调度器已启动: 每天 {hour:02d}:{minute:02d} 自动执行流水线 (Ctrl+C 退出)")
    logger.info("调度器已启动: daily_run_time=%s", settings.daily_run_time)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n调度器已停止")
        logger.info("调度器收到中断信号, 正常退出")


# ============================================================
# CLI 入口
# ============================================================


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数。

    Returns:
        argparse.Namespace: 含 run_once / skip_push / verbose 等开关。
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="销售线索智能分析与分发助手 —— 每日定时画像分层/RAG匹配/钉钉触达闭环",
    )
    parser.add_argument(
        "--run-once", action="store_true",
        help="一次性跑通完整流水线并打印 summary(不进入定时模式)",
    )
    parser.add_argument(
        "--skip-push", action="store_true",
        help="跳过钉钉推送环节(数据仍落库, 可稍后重推)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="打开调试级日志(默认 INFO)",
    )
    return parser.parse_args(argv)


def setup_logging(verbose: bool = False) -> None:
    """配置全局日志: verbose 时 DEBUG, 否则 INFO; 统一格式。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口: --run-once 一次性执行, 否则进入定时模式。

    Args:
        argv: 命令行参数列表; None 表示取 sys.argv[1:]。

    Returns:
        int: 进程退出码(0 正常; 1 数据缺失/致命错误)。
    """
    args = parse_args(argv)
    setup_logging(verbose=args.verbose)

    print("=" * 56)
    print("销售线索智能分析与分发助手")
    print(f"mock_mode={settings.mock_mode} | 大模型={settings.llm_model}"
          f" | 钉钉={'已配置' if settings.dingtalk_webhook_url else '未配置(跳过推送)'}")
    print("=" * 56)

    if args.run_once:
        logger.info("--run-once 模式: 执行一次完整流水线")
        try:
            state = run_pipeline(args)
        except Exception as exc:  # noqa: BLE001 —— 最外层兜底
            logger.error("致命错误: %s", exc)
            print(f"[错误摘要] 流水线致命异常: {exc}")
            return 1
        print()
        print(lgf.build_summary_text(state))
        return 0

    # 默认: 定时模式
    start_scheduler(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())