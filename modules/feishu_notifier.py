# -*- coding: utf-8 -*-
"""飞书推送器(feishu_notifier): 把日报/分配明细推送到飞书群自定义机器人。

职责(与 dingtalk_notifier 对齐, 便于上层统一调用):
    1. send_daily_report: 推送销售线索智能日报(标题 + 正文文本块)到飞书群机器人;
    2. send_assignment_batch: 推送分配明细(客户|推荐销售|理由 文本块)。

加签规范(飞书开放平台「自定义机器人-签名校验」, 算法与钉钉同款):
    1. 取 timestamp(秒)与 secret, 拼接字符串 timestamp + "\\n" + secret;
    2. 用 HMAC-SHA256 对上述字符串做摘要, key 为 secret;
    3. 对摘要做 Base64 编码;
    4. 再做 URL 编码(urllib.parse.quote_plus);
    5. 拼到 webhook URL 之后追加  &timestamp=...&sign=... 。
    说明: 钉钉用毫秒时间戳, 飞书官方文档用秒时间戳 —— 两通道各自独立实现,
    互不影响; 若飞书官方对其安全设置有更新(如直接使用"自定义关键词"),
    两种安全设置取其一即可, 本实现以「签名校验」为准, 未配置 secret 时不加签。

消息体(飞书 post 富文本, 与钉钉 markdown 不同):
    {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [[{"tag": "text", "text": summary_text}]]
                }
            }
        }
    }
    POST JSON, 超时 10s。

技术选型(任务规定): 仅标准库 hmac / hashlib / base64 / urllib.parse /
urllib.request, 无第三方 HTTP 依赖。

健壮性(全部"失败不抛异常、不中断调用方"):
    ① webhook 未配置 → 打印提示并返回 False;
    ② 推送失败/超时 → 记日志返回 False;
    ③ 响应码非 200 或 JSON code != 0 → 记日志返回 False;
        (飞书机器人响应中业务码字段为 "code", 与钉钉 "errcode" 不同)
    ④ 单条分配明细格式异常 → 该行按安全占位文本渲染, 不中断整批。

依赖方向(单向, 与全项目一致):
    notifier → config.settings(读 feishu_webhook_url/feishu_secret)
   与 lead_assigner.AssignmentResult(仅类型标注); 本模块不反向依赖调度/数据模块。
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

# HTTP 推送超时(秒, 与钉钉一致)
_REQUEST_TIMEOUT: int = 10
# 飞书响应业务码键: {"code": 0, "msg": "success"}(与钉钉 errcode 不同)
_FEISHU_CODE_KEY: str = "code"


# ============================================================
# 加签(飞书自定义机器人「签名校验」, 算法与钉钉同款, 时间戳单位不同)
# ============================================================

def _generate_sign(secret: str, timestamp_sec: Optional[int] = None) -> str:
    """按飞书规范生成加签参数 sign。

    Args:
        secret: 机器人签名校验密钥。
        timestamp_sec: 秒时间戳; 不传则取当前时间(便于单测注入固定值)。

    Returns:
        str: URL 编码后的 sign 值(可直接拼进 query string)。

    Raises:
        ValueError: secret 为空时抛出(调用方需保证非空才调用本函数)。

    Example:
        算法:C = timestamp_sec + "\\n" + secret;
             digest = HMAC-SHA256(key=secret, msg=C);
             sign = quote_plus(Base64(digest))
    """
    secret = (secret or "").strip()
    if not secret:
        raise ValueError("feishu secret 为空, 无法加签")
    ts = timestamp_sec if timestamp_sec is not None else int(time.time())
    string_to_sign: str = f"{ts}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    return sign


def _build_signed_url(
    webhook: str, secret: Optional[str] = None, timestamp_sec: Optional[int] = None
) -> str:
    """在 webhook URL 上追加加签参数(仅当 secret 非空时)。

    Args:
        webhook: 机器人 webhook 地址(含查询参数)。
        secret: 签名校验密钥, 可为空(为空则不加签, 只依赖机器人自身的
                「自定义关键词」安全设置)。
        timestamp_sec: 秒时间戳, 供测试注入。

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
    ts = timestamp_sec if timestamp_sec is not None else int(time.time())
    sign = _generate_sign(secret, ts)
    return f"{url}{separator}timestamp={ts}&sign={sign}"


# ============================================================
# 文案构造(飞书 post 文本块)
# ============================================================

def build_assignment_table_text(assignments: List) -> str:
    """构造分配明细的纯文本表格(客户|推荐销售|理由), 供飞书 post 文本块展示。

    Args:
        assignments: AssignmentResult 列表(可为空; 兼容任意含
            customer_name/sales_name/match_reason 属性的对象或 dict)。

    Returns:
        str: 文本表格; 空列表时返回"本期无分配明细"占位。
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
        text = str(value).strip().replace("\n", " ")
        # 理由字段过长时截断, 避免飞书消息被撑爆
        if name == "match_reason" and len(text) > 60:
            text = text[:60] + "…"
        return text

    lines = ["客户 | 推荐销售 | 理由"]
    lines += [
        f"{_field(a, 'customer_name')} | {_field(a, 'sales_name')} | {_field(a, 'match_reason')}"
        for a in assignments
    ]
    return "\n".join(lines)


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
        text: 正文文本(放入 post 文本块)。
        title: 消息标题(放 post 的 title 字段)。
        webhook: 已 trim 的 webhook 地址(非空, 由调用方保证)。
        secret: 签名校验密钥(可为空, 为空则不加签)。

    Returns:
        bool: 是否推送成功。
    """
    url = _build_signed_url(webhook, secret)
    payload: Dict[str, object] = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [[{"tag": "text", "text": text}]],
                }
            }
        },
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
        logger.warning("飞书推送失败(网络/超时/HTTP 异常): %s", exc)
        return False

    if status != 200:
        logger.warning("飞书推送失败: HTTP %s, body=%s", status, raw[:500])
        return False
    try:
        body = json.loads(raw or "{}")
    except Exception as exc:  # noqa: BLE001 —— 响应非 JSON 视为失败
        logger.warning("飞书推送失败: 响应非法 JSON(%s), raw=%s", exc, raw[:500])
        return False
    if not isinstance(body, dict) or body.get(_FEISHU_CODE_KEY) != 0:
        logger.warning("飞书推送失败: code=%s, msg=%s",
                       body.get(_FEISHU_CODE_KEY) if isinstance(body, dict) else "?",
                       (body.get("msg") if isinstance(body, dict) else raw)[:500])
        return False
    logger.info("飞书推送成功: title=%s, msg=%s", title, body.get("msg"))
    return True


# ============================================================
# 对外主入口(与 dingtalk_notifier 对齐的契约)
# ============================================================

def send_daily_report(summary_text: str, title: str = "销售线索智能日报") -> bool:
    """推送日报(飞书 post 富文本)到飞书群机器人; 返回是否推送成功。

    Args:
        summary_text: 日报正文文本(可由 dingtalk_notifier.build_daily_report_text
                      等构造好的文本直接复用, 飞书以纯文本块展示)。
        title: 消息标题(飞书 post 的 title 字段), 默认"销售线索智能日报"。

    Returns:
        bool: 成功 True; webhook 未配置 / 推送失败 / 超时 / 响应异常 → False(不抛异常)。

    Note:
        webhook 未配置时打印提示并返回 False —— 这是 mock 模式(开箱即跑)
        下的预期行为: 分析/分配照常落库, 推送环节静默跳过, 不中断调度。
    """
    summary_text = (summary_text or "").strip()
    webhook = (settings.feishu_webhook_url or "").strip()
    secret = (settings.feishu_secret or "").strip()

    # ① webhook 未配置 → 打印提示返回 False, 不抛异常
    if not webhook:
        print(
            "[feishu_notifier] 未配置 FEISHU_WEBHOOK_URL, 跳过日报推送 "
            "(mock 模式无需配置); 日报数据已落库可稍后重推"
        )
        logger.info("webhook 未配置(mock 模式), 跳过日报推送")
        return False

    text = summary_text or "本期无日报内容"
    return _do_push(text=text, title=title, webhook=webhook, secret=secret)


def send_assignment_batch(assignments: List) -> bool:
    """推送分配明细(客户|推荐销售|理由 文本块)到飞书群机器人; 返回是否推送成功。

    Args:
        assignments: AssignmentResult 列表(可为空)。

    Returns:
        bool: 成功 True; webhook 未配置 / 推送失败 → False(不抛异常)。
    """
    assignments = list(assignments or [])
    webhook = (settings.feishu_webhook_url or "").strip()
    secret = (settings.feishu_secret or "").strip()

    # ① webhook 未配置 → 打印提示返回 False, 不抛异常
    if not webhook:
        print(
            "[feishu_notifier] 未配置 FEISHU_WEBHOOK_URL, 跳过分配明细推送 "
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