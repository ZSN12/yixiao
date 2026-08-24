# -*- coding: utf-8 -*-
"""钉钉推送器(dingtalk_notifier): 把日报/分配明细推送到钉钉群自定义机器人。

职责:
    1. send_daily_report: 推送 markdown 版销售线索智能日报(客户数/画像分层统计/
       分配摘要/更新时间)到钉钉群机器人;
    2. send_assignment_batch: 推送分配明细表格(客户|推荐销售|理由)。

加签规范(钉钉开放平台「自定义机器人安全设置-加签」, 社招/校招常考, 必须正确):
    1. 取 timestamp(毫秒)与 secret, 拼接字符串 "timestamp\nsecret"(注意: 钉钉文档
       括号内明确要求原文是  timestamp+"\n"+secret, 而非 "secret\ntimestamp");
    2. 用 HMAC-SHA256 对上述字符串做摘要, key 为 secret;
    3. 对摘要做 Base64 编码;
    4. 再做 URL 编码(urllib.parse.quote_plus);
    5. 拼到 webhook URL:  https://oapi.dingtalk.com/robot/send?access_token=xxx
       之后追加  &timestamp=...&sign=... 。

技术选型(任务规定):
    - 只用标准库 hmac / hashlib / base64 / urllib.parse / urllib.request,
      不依赖第三方 HTTP 库(具备连测试环境也能在纯标准库环境运行的能力);
    - 消息体 {"msgtype": "markdown", "markdown": {"title": title, "text": text}},
      POST JSON(Content-Type: application/json; charset=utf-8), 超时 10s。

健壮性(逐条落实, 全部"失败不抛异常、不中断调用方"):
    ① webhook 未配置(空串/None/纯空白)→ 打印提示并返回 False;
    ② 推送失败/超时(抛 URLError/TimeoutError/OSError 等)→ 记日志返回 False;
       (日报数据已落库, 调度器可稍后重推)
    ③ 响应码非 200 或 JSON errcode != 0 → 记日志返回 False;
    ④ 单条分配明细格式异常(缺字段/超长)→ 该行按安全的占位文本渲染, 不中断整批。

依赖方向(单向, 与全项目一致):
    notifier → config.settings(读 webhook/secret) 与 lead_assigner.AssignmentResult(类型标注);
    本模块不反向依赖调度/数据模块。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from config.settings import settings

# lead_assigner 为可选依赖: 导入失败时(仅类型标注用途)不阻塞本模块加载
try:
    from modules.lead_assigner import AssignmentResult  # noqa: F401 —— 仅类型标注
except Exception:  # noqa: BLE001 —— 类型标注依赖缺失时降级
    AssignmentResult = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# HTTP 推送超时(秒, 任务规定)
_REQUEST_TIMEOUT: int = 10
# 钉钉响应码键: {"errcode": 0, "errmsg": "ok"}
_DINGTALK_ERRCODE_KEY: str = "errcode"


# ============================================================
# 加签(钉钉自定义机器人「加签」安全设置, 面试必考点)
# ============================================================


def _generate_sign(secret: str, timestamp_ms: Optional[int] = None) -> str:
    """按钉钉规范生成加签参数 sign。

    Args:
        secret: 机器人加签密钥(SEC 开头)。
        timestamp_ms: 毫秒时间戳; 不传则取当前时间(便于单测注入固定值)。

    Returns:
        str: URL 编码后的 sign 值(可直接拼进 query string)。

    Raises:
        ValueError: secret 为空时抛出(调用方需保证非空才调用本函数)。

    Example:
        算法:C = timestamp_ms + "\\n" + secret;
             digest = HMAC-SHA256(key=secret, msg=C);
             sign = quote_plus(Base64(digest))
    """
    secret = (secret or "").strip()
    if not secret:
        raise ValueError("dingtalk secret 为空, 无法加签")
    ts = timestamp_ms if timestamp_ms is not None else int(round(time.time() * 1000))
    string_to_sign: str = f"{ts}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    return sign


def _build_signed_url(
    webhook: str, secret: Optional[str] = None, timestamp_ms: Optional[int] = None
) -> str:
    """在 webhook URL 上追加加签参数(仅当 secret 非空时)。

    Args:
        webhook: 机器人 webhook 地址(含 access_token 查询参数)。
        secret: 加签密钥, 可为空(为空则不加签, 只依赖机器人自身的
                「自定义关键词」或「IP 白名单」安全设置)。
        timestamp_ms: 毫秒时间戳, 供测试注入。

    Returns:
        str: 组装好的完整推送 URL。
    """
    url = (webhook or "").strip()
    secret = (secret or "").strip()
    if not url:
        return ""
    if not secret:
        return url
    separator = "&" if "?" in url else "?"
    ts = timestamp_ms if timestamp_ms is not None else int(round(time.time() * 1000))
    sign = _generate_sign(secret, ts)
    return f"{url}{separator}timestamp={ts}&sign={sign}"


# ============================================================
# 文案构造(日报 / 分配明细)
# ============================================================


def build_daily_report_text(
    customer_count: int,
    profile_stats: Optional[Dict[str, Dict[str, int]]] = None,
    assignment_summary: Optional[Dict[str, str]] = None,
    updated_at: Optional[str] = None,
) -> str:
    """构造日报 markdown 正文(不含标题; 标题由调用方/消息体 title 决定)。

    Args:
        customer_count: 本期纳入分析的客户总数。
        profile_stats: 画像分层统计, 形如
            {"意向": {"高": 3, "中": 5, "低": 2}, "流失": {"高": 1, "中": 3, "低": 6}};
            缺失等级键按 0 补齐(保证三类都展示)。
        assignment_summary: 分配摘要, 形如
            {"recommend": "张三(5单)/李四(3单)…", "needs_human": "2家待人工分配"}。
        updated_at: 生成时间(如 "2024-08-19 08:30:00"); 为空则用当前时间。

    Returns:
        str: markdown 文本(钉钉 markdown 需带 \n 换行, 表格用 markdown 语法)。
    """
    profile_stats = profile_stats or {}
    intent = profile_stats.get("意向") or {}
    churn = profile_stats.get("流失") or {}
    intent = {"高": intent.get("高", 0), "中": intent.get("中", 0), "低": intent.get("低", 0)}
    churn = {"高": churn.get("高", 0), "中": churn.get("中", 0), "低": churn.get("低", 0)}
    assignment_summary = assignment_summary or {}
    updated_at = (updated_at or "").strip() or time.strftime("%Y-%m-%d %H:%M:%S")

    lines: List[str] = [
        "### 客户概况",
        f"- 本期客户总数: **{customer_count}** 家",
        "",
        "### 画像分层统计",
        f"- 意向等级: 高 **{intent['高']}** / 中 **{intent['中']}** / 低 **{intent['低']}**",
        f"- 流失风险: 高 **{churn['高']}** / 中 **{churn['中']}** / 低 **{churn['低']}**",
        "",
        "### 分配摘要",
    ]
    recommend = (assignment_summary.get("recommend") or "").strip()
    needs_human = (assignment_summary.get("needs_human") or "").strip()
    if recommend:
        lines.append(f"- 推荐销售: {recommend}")
    if needs_human:
        lines.append(f"- 待人工分配: {needs_human}")
    if not recommend and not needs_human:
        lines.append("- 本期无分配结果")
    lines.extend([
        "",
        "---",
        f"> 数据更新时间: {updated_at}",
    ])
    return "\n".join(lines)


def build_assignment_table_text(assignments: List) -> str:
    """构造分配明细 markdown 表格(客户|推荐销售|理由), 附机读数据。

    Args:
        assignments: AssignmentResult 列表(可为空; 兼容任意含
            customer_name/sales_name/match_reason 属性的对象或 dict)。

    Returns:
        str: markdown 表格文本; 空列表时返回"无分配明细"占位。
    """
    assignments = list(assignments or [])
    if not assignments:
        return "本期无分配明细"

    def _field(item, name: str, fallback: str = "-") -> str:
        """从对象或 dict 中安全取字段; 缺失/异常用占位, 单行不中断整批。"""
        try:
            if isinstance(item, dict):
                value = item.get(name)
            else:
                value = getattr(item, name)
        except Exception:  # noqa: BLE001 —— 字段缺失等异常统一走占位
            value = None
        if value is None or str(value).strip() == "":
            return fallback
        text = str(value).strip().replace("|", "\\|").replace("\n", " ")
        # 理由字段过长时截断, 避免钉钉表格被撑爆
        if name == "match_reason" and len(text) > 60:
            text = text[:60] + "…"
        return text

    header = "| 客户 | 推荐销售 | 理由 |"
    divider = "| --- | --- | --- |"
    body = [
        f"| {_field(a, 'customer_name')} | {_field(a, 'sales_name')} | {_field(a, 'match_reason')} |"
        for a in assignments
    ]
    return "\n".join([header, divider] + body)


# ============================================================
# 实际推送
# ============================================================


def _do_push(
    text: str,
    title: str,
    webhook: str,
    secret: str,
) -> bool:
    """执行单次 HTTP 推送(加签 + POST JSON + 响应校验)。

    Args:
        text: markdown 正文。
        title: 消息标题。
        webhook: 已 trim 的 webhook 地址(非空, 由调用方保证)。
        secret: 加签密钥(可为空, 为空则不加签)。

    Returns:
        bool: 是否推送成功。
    """
    url = _build_signed_url(webhook, secret)
    payload: Dict[str, object] = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 —— 覆盖 URLError/TimeoutError/OSError/HTTPError 等
        logger.warning("钉钉推送失败(网络/超时/HTTP 异常): %s", exc)
        return False

    if status != 200:
        logger.warning("钉钉推送失败: HTTP %s, body=%s", status, raw[:500])
        return False
    try:
        body = json.loads(raw or "{}")
    except Exception as exc:  # noqa: BLE001 —— 响应非 JSON 视为失败
        logger.warning("钉钉推送失败: 响应非法 JSON(%s), raw=%s", exc, raw[:500])
        return False
    if not isinstance(body, dict) or body.get(_DINGTALK_ERRCODE_KEY) != 0:
        logger.warning("钉钉推送失败: errcode=%s, errmsg=%s",
                       body.get(_DINGTALK_ERRCODE_KEY) if isinstance(body, dict) else "?",
                       (body.get("errmsg") if isinstance(body, dict) else raw)[:500])
        return False
    logger.info("钉钉推送成功: title=%s, errmsg=%s", title, body.get("errmsg"))
    return True


# ============================================================
# 对外主入口(任务规定的函数契约)
# ============================================================


def send_daily_report(summary_text: str, title: str = "销售线索智能日报") -> bool:
    """推送日报 markdown 到钉钉群机器人; 返回是否推送成功。

    Args:
        summary_text: 日报 markdown 正文(由调度器传入 build_daily_report_text
                      等构造好的文本; 为空时使用默认空内容占位)。
        title: 消息标题(也作为钉钉 markdown 消息的标题字段), 默认"销售线索智能日报"。

    Returns:
        bool: 成功 True; webhook 未配置 / 推送失败 / 超时 / 响应异常 → False(不抛异常)。

    Note:
        webhook 未配置时打印提示并返回 False —— 这是 mock 模式(开箱即跑)
        下的预期行为: 分析/分配照常落库, 推送环节静默跳过, 不中断调度。
    """
    summary_text = (summary_text or "").strip()
    webhook = (settings.dingtalk_webhook_url or "").strip()
    secret = (settings.dingtalk_secret or "").strip()

    # ① webhook 未配置 → 打印提示返回 False, 不抛异常
    if not webhook:
        print(
            "[dingtalk_notifier] 未配置 DINGTALK_WEBHOOK_URL, 跳过日报推送 "
            "(mock 模式无需配置); 日报数据已落库可稍后重推"
        )
        logger.info("webhook 未配置(mock 模式), 跳过日报推送")
        return False

    text = summary_text or "本期无日报内容"
    return _do_push(text=text, title=title, webhook=webhook, secret=secret)


def send_assignment_batch(assignments: List) -> bool:
    """推送分配明细(客户|推荐销售|理由 表格)到钉钉群机器人; 返回是否推送成功。

    Args:
        assignments: AssignmentResult 列表(可为空)。

    Returns:
        bool: 成功 True; webhook 未配置 / 推送失败 → False(不抛异常)。
    """
    assignments = list(assignments or [])
    webhook = (settings.dingtalk_webhook_url or "").strip()
    secret = (settings.dingtalk_secret or "").strip()

    # ① webhook 未配置 → 打印提示返回 False, 不抛异常
    if not webhook:
        print(
            "[dingtalk_notifier] 未配置 DINGTALK_WEBHOOK_URL, 跳过分配明细推送 "
            "(mock 模式无需配置)"
        )
        logger.info("webhook 未配置(mock 模式), 跳过分配明细推送")
        return False

    text = build_assignment_table_text(assignments)
    return _do_push(
        text=text,
        title=f"销售线索分配明细(共 {len(assignments)} 家)",
        webhook=webhook,
        secret=secret,
    )