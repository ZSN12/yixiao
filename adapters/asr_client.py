# -*- coding: utf-8 -*-
"""ASR 客户端抽象层(asr_client): 语音 → 带说话人标签的分段文本。

职责: 把电话录音转成统一的 AsrSegment 列表(每条含 speaker/text/时间戳/置信度),
不关心「谁是销售、谁是客户」—— 角色判定是 speaker_role_resolver 的职责。

Provider 优先级(与全项目渐进降级一致):
1. mock(P0): 读 phone_call_transcript_demo.json, 不调外部 API, CI 零成本跑通;
2. aliyun / tencent / xfyun(P1): 接真实 ASR 服务商(需带说话人分离能力)。

设计要点:
- 统一入口 transcribe(audio_path, provider), 业务方无感知 provider 差异;
- 真实 provider 未实现时, 显式抛出 NotImplementedError 并在日志说明,
  而不是静默返回空(避免误判「没说话」)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# 项目根目录(用于解析相对路径的演示转写文件)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRANSCRIPT_PATH = PROJECT_ROOT / "data" / "real" / "phone_call_transcript_demo.json"


@dataclass
class AsrSegment:
    """一段带说话人标签的转写文本(ASR 统一输出格式)。"""

    speaker: str            # "Speaker_0" | "Speaker_1" | ...(说话人匿名标签)
    text: str
    start_ms: int
    end_ms: int
    confidence: float = 1.0


def transcribe(audio_path: str, *, provider: str = "mock") -> List[AsrSegment]:
    """统一 ASR 入口: 录音 → List[AsrSegment]。

    Args:
        audio_path: 音频文件路径(或空串, mock 模式下忽略)。
        provider: ASR 提供商, mock | aliyun | tencent | xfyun。

    Returns:
        list[AsrSegment]: 按时间排序的分段转写结果。

    Raises:
        NotImplementedError: 指定了未实现的真实 provider。
    """
    provider = (provider or "mock").lower()
    if provider == "mock":
        # mock 模式: 不依赖 audio_path, 由调用方通过 transcript_demo_id 定位
        # 具体演示转写; 这里返回空, 由 phone_call_adapter 负责加载演示转写。
        logger.info("ASR(mock): 不调外部 API, 转写结果由演示转写文件提供")
        return []
    if provider in ("aliyun", "tencent", "xfyun"):
        raise NotImplementedError(
            f"ASR provider '{provider}' 尚未接入(P1 阶段)。"
            f"当前仅支持 mock; 请改用 provider='mock' 或实现 {provider} 客户端。"
        )
    raise ValueError(f"未知的 ASR provider: {provider}")


def load_mock_transcript(transcript_demo_id: str, transcript_path: Optional[str] = None) -> List[AsrSegment]:
    """从演示转写文件加载某个通话的分段转写(mock ASR 输出)。

    演示转写文件为 JSON 对象, 以 call_id(transcript_demo_id) 为键:
        {
          "CALL001": [
            {"speaker": "Speaker_0", "text": "...", "start_ms": 0, "end_ms": 2000, "confidence": 0.99},
            ...
          ]
        }

    Args:
        transcript_demo_id: 通话 ID(对应演示转写文件中的键)。
        transcript_path: 转写文件路径(默认 data/real/phone_call_transcript_demo.json)。

    Returns:
        list[AsrSegment]: 分段转写列表; 找不到时返回空列表并记警告。
    """
    path = Path(transcript_path) if transcript_path else DEFAULT_TRANSCRIPT_PATH
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        logger.warning("演示转写文件不存在: %s", path)
        return []

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("读取演示转写失败(%s): %s", path, exc)
        return []

    raw_segments = data.get(transcript_demo_id) or []
    segments: List[AsrSegment] = []
    for seg in raw_segments:
        try:
            segments.append(AsrSegment(
                speaker=str(seg.get("speaker") or "").strip(),
                text=str(seg.get("text") or ""),
                start_ms=int(seg.get("start_ms") or 0),
                end_ms=int(seg.get("end_ms") or 0),
                confidence=float(seg.get("confidence", 1.0)),
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("跳过非法分段 %r: %s", seg, exc)
    return segments
