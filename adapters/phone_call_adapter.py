# -*- coding: utf-8 -*-
"""电话录音适配器(phone_call_adapter): 录音 → ChatRecord(标准契约)。

职责: 把电话录音(含元数据 manifest)转成现有 ChatRecord 格式, 且每条
ChatMessage.role 必须是 "销售" 或 "客户", 这样 profile_analyzer 的
「说话者区分 + 价格时间衰减」才能生效。

对标现有企微适配(crm_data_adapter.load_chat_records_from_export):
企微适配做的是「员工/外部联系人 → 销售/客户」; 电话适配做的是
「Speaker_0/Speaker_1 → 销售/客户」, 角色判定在 adapter 层完成。

处理流程:
    1. ASR(mock 或真实 provider)→ List[AsrSegment];
    2. 三层角色判定(speaker_role_resolver)→ RoleResolution;
    3. 分段 → ChatMessage(只保留 role in {"销售","客户"}, 合并连续短句);
    4. 组装 ChatRecord(record_id=call_id, chat_time 截取前 10 位对齐 YYYY-MM-DD)。

独立运行(演示):
    python adapters/phone_call_adapter.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 项目根: 本文件位于 <项目根>/adapters/phone_call_adapter.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 直接运行本文件(python adapters/phone_call_adapter.py)时, 把项目根加入 sys.path
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.data_loader import ChatMessage, ChatRecord

from adapters.asr_client import AsrSegment, load_mock_transcript, transcribe
from adapters.speaker_role_resolver import RoleResolution, resolve

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "data" / "real" / "phone_call_manifest.json"


# ============================================================
# 对外 API
# ============================================================


def load_chat_from_call(manifest: Dict) -> Tuple[ChatRecord, RoleResolution]:
    """单通电话 → ChatRecord + 角色判定元信息。

    Args:
        manifest: 通话元数据(见 data/real/phone_call_manifest.json 示例),
            必填 call_id/customer_id/call_time; 建议 direction/sales_id。

    Returns:
        tuple[ChatRecord, RoleResolution]: 标准会话记录 + 角色判定结果。
    """
    # 1. ASR: mock 读演示转写, 真实 provider 调 asr_client.transcribe
    call_id = str(manifest.get("call_id") or "")
    audio = manifest.get("audio_path") or manifest.get("audio_url") or ""
    transcript_demo_id = str(manifest.get("transcript_demo_id") or call_id)

    try:
        from config.settings import settings
        provider = settings.asr_provider
    except Exception:  # noqa: BLE001
        provider = "mock"

    if provider == "mock" or not audio:
        segments = load_mock_transcript(transcript_demo_id)
    else:
        segments = transcribe(audio, provider=provider)

    # 2. 角色判定
    resolution = resolve(segments, manifest)

    # 3. 分段 → ChatMessage
    messages = segments_to_messages(segments, resolution.speaker_roles)

    # 4. 组装 ChatRecord
    chat_time = str(manifest.get("call_time") or "")
    record = ChatRecord(
        record_id=call_id or f"CALL{hash(tuple(sorted(manifest.items()))) % 1000:03d}",
        customer_id=str(manifest.get("customer_id") or ""),
        sales_id=(str(manifest.get("sales_id") or "").strip()) or None,
        chat_time=chat_time[:10],  # 与 mock 对齐 YYYY-MM-DD
        messages=messages,
    )
    logger.info(
        "电话转写完成: call_id=%s, 分段=%d, 角色=%s(method=%s, conf=%.2f)",
        call_id, len(segments), resolution.speaker_roles,
        resolution.method, resolution.confidence,
    )
    return record, resolution


def load_chats_from_manifest_file(path: str) -> List[Tuple[ChatRecord, RoleResolution]]:
    """批量: 从 manifest 文件(JSON 数组)加载多通电话。

    Args:
        path: manifest 文件路径(JSON 数组, 每项为一条通话元数据)。

    Returns:
        list[tuple[ChatRecord, RoleResolution]]: 每通电话的会话 + 角色判定。
    """
    manifest_file = Path(path)
    if not manifest_file.is_absolute():
        manifest_file = PROJECT_ROOT / manifest_file
    if not manifest_file.exists():
        raise FileNotFoundError(f"电话录音 manifest 不存在: {manifest_file}")

    with open(manifest_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"电话录音 manifest 应为 JSON 数组: {manifest_file}")

    results: List[Tuple[ChatRecord, RoleResolution]] = []
    for item in data:
        try:
            results.append(load_chat_from_call(item))
        except Exception as exc:  # noqa: BLE001 —— 单条失败跳过, 不中断整体
            logger.warning("通话 %s 解析失败, 跳过: %s", item.get("call_id"), exc)
    logger.info("电话 manifest 加载完成: %s, 共 %d 通", manifest_file, len(results))
    return results


def segments_to_messages(
    segments: List[AsrSegment],
    roles: Dict[str, str],
    merge_gap_ms: int = 5000,
) -> List[ChatMessage]:
    """分段 → ChatMessage: 映射角色 + 合并同一 speaker 的连续短句。

    Args:
        segments: ASR 分段列表(未排序亦可)。
        roles: 说话人 → 业务角色映射({"Speaker_0": "销售", ...})。
        merge_gap_ms: 同一说话人相邻分段间隔小于该毫秒数时合并。

    Returns:
        list[ChatMessage]: 只保留 role in {"销售","客户"} 的消息, 按时间顺序。
    """
    ordered = sorted(segments, key=lambda s: s.start_ms)
    messages: List[ChatMessage] = []
    last_speaker: Optional[str] = None
    last_end_ms: int = 0

    for seg in ordered:
        role = roles.get(seg.speaker, "")
        if role not in ("销售", "客户"):
            # 无法判定或「未知」角色: 跳过(不污染下游角色区分)
            continue
        text = (seg.text or "").strip()
        if not text:
            continue
        # 合并: 与上一条同角色(同一说话人)且间隔 < merge_gap_ms
        if messages and seg.speaker == last_speaker and (seg.start_ms - last_end_ms) < merge_gap_ms:
            messages[-1].content = messages[-1].content + text
        else:
            messages.append(ChatMessage(role=role, content=text))
        last_speaker = seg.speaker
        last_end_ms = seg.end_ms

    return messages


# ============================================================
# 端到端实证(对标 crm_data_adapter.run_real_data_demo)
# ============================================================


def run_phone_call_demo(manifest_path: Optional[str] = None) -> Dict:
    """端到端实证: 电话录音 → ChatRecord → 画像分析(说话者区分 + 价格衰减)。

    流程(与 mock 流水线同一调用路径):
        1. manifest → List[ChatRecord](角色判定在 adapter 层完成);
        2. build_chat_map → analyze_customers_batch(规则引擎);
        3. 返回实证摘要(通话数/角色判定/意向分层)。

    Args:
        manifest_path: manifest 文件路径(默认 data/real/phone_call_manifest.json)。

    Returns:
        dict: {"call_count", "role_resolutions", "analyzed", "intention_stats"}。
    """
    from modules import data_loader
    from modules import profile_analyzer

    path = manifest_path or str(DEFAULT_MANIFEST_PATH)
    records_with_roles = load_chats_from_manifest_file(path)
    chat_records = [rec for rec, _ in records_with_roles]
    chat_map = data_loader.build_chat_map(chat_records)

    # 用 mock 客户主数据(客户画像分析需要 Customer 对象)
    customers = data_loader.load_customers()
    # 只分析 manifest 中出现的客户(或全部 mock 客户)
    involved_cids = {rec.customer_id for rec in chat_records}
    targets = [c for c in customers if c.customer_id in involved_cids] or customers

    analysis = profile_analyzer.analyze_customers_batch(targets, chat_map)

    role_summaries = [
        {
            "call_id": rec.record_id,
            "method": res.method,
            "confidence": res.confidence,
            "needs_review": res.needs_review(),
            "roles": res.speaker_roles,
            "notes": res.notes,
        }
        for rec, res in records_with_roles
    ]
    intention_stats: Dict[str, int] = {}
    for r in analysis.values():
        intention_stats[r.intention_level] = intention_stats.get(r.intention_level, 0) + 1

    logger.info(
        "电话录音实证完成: 通话 %d 通, 分析 %d 家, 意向 %s",
        len(chat_records), len(analysis), intention_stats,
    )
    return {
        "call_count": len(chat_records),
        "role_resolutions": role_summaries,
        "analyzed": len(analysis),
        "intention_stats": intention_stats,
    }


# ============================================================
# 命令行入口(演示)
# ============================================================


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    summary = run_phone_call_demo()
    print("\n=== 电话录音适配实证 ===")
    print(f"通话数: {summary['call_count']}")
    print(f"分析客户数: {summary['analyzed']}")
    print(f"意向分层: {summary['intention_stats']}")
    print("\n角色判定明细:")
    for item in summary["role_resolutions"]:
        review = " ⚠️需复核" if item["needs_review"] else ""
        print(
            f"  {item['call_id']}: {item['roles']} "
            f"(method={item['method']}, conf={item['confidence']:.2f}){review}"
        )
