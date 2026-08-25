# -*- coding: utf-8 -*-
"""ASR 客户端抽象层(asr_client): 语音 → 带说话人标签的分段文本。

职责: 把电话录音转成统一的 AsrSegment 列表(每条含 speaker/text/时间戳/置信度),
不关心「谁是销售、谁是客户」—— 角色判定是 speaker_role_resolver 的职责。

Provider 分层(与全项目渐进降级一致):
1. mock(P0 已交付): 读 phone_call_transcript_demo.json, 不调外部 API, CI 零成本;
2. 真实服务商(P1 框架): 通过「可插拔 Provider 注册表」接入阿里云/腾讯/讯飞,
   统一实现 AsrProvider 协议, 各厂商只需补「网络调用 + 响应解析」两段。

设计要点:
- 统一入口 transcribe(audio_path, provider), 业务方无感知 provider 差异;
- 真实 provider 未实现时, 显式抛出 NotImplementedError 并在日志说明,
  而不是静默返回空(避免误判「没说话」);
- 厂商客户端通过 register_provider() 注册, 扩展新厂商无需改 transcribe 逻辑。
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

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


class AsrProvider(ABC):
    """真实 ASR 服务商客户端协议(P1 接入契约)。

    实现一个厂商 = 继承本类并实现 transcribe(audio_path) → List[AsrSegment],
    然后在模块导入时调用 register_provider() 注册。业务方统一走
    transcribe(audio_path, provider=...) 入口, 无感知厂商差异。
    """

    name: str = "abstract"   # 注册名(与 settings.asr_provider 对齐)

    @abstractmethod
    def transcribe(self, audio_path: str) -> List[AsrSegment]:
        """把音频转成带说话人标签的分段转写。

        Args:
            audio_path: 音频文件路径(本地或 URL)。

        Returns:
            list[AsrSegment]: 按时间排序的分段(含 speaker 标签)。

        Raises:
            RuntimeError: 网络/凭证/解析失败 —— 由调用方降级或标记 review。
        """
        raise NotImplementedError


# 厂商注册表: {provider_name: AsrProvider 实例}
_PROVIDERS: Dict[str, AsrProvider] = {}


def register_provider(provider: AsrProvider) -> None:
    """注册一个 ASR 服务商客户端(幂等, 后注册覆盖同名)。"""
    _PROVIDERS[provider.name] = provider
    logger.info("已注册 ASR provider: %s", provider.name)


def get_provider(name: str) -> Optional[AsrProvider]:
    """按名取已注册的 ASR 服务商客户端。"""
    return _PROVIDERS.get(name)


def transcribe(audio_path: str, *, provider: str = "mock") -> List[AsrSegment]:
    """统一 ASR 入口: 录音 → List[AsrSegment]。

    Args:
        audio_path: 音频文件路径(或空串, mock 模式下忽略)。
        provider: ASR 提供商, mock | aliyun | tencent | xfyun。

    Returns:
        list[AsrSegment]: 按时间排序的分段转写结果。

    Raises:
        NotImplementedError: 指定了未实现/未注册的真实 provider。
    """
    provider = (provider or "mock").lower()
    if provider == "mock":
        logger.info("ASR(mock): 不调外部 API, 转写结果由演示转写文件提供")
        return []

    impl = _PROVIDERS.get(provider)
    if impl is None:
        raise NotImplementedError(
            f"ASR provider '{provider}' 尚未接入。"
            f"当前仅支持 mock; 请实现 {provider} 客户端并 register_provider() 注册, "
            f"或改用 provider='mock'。"
        )
    return impl.transcribe(audio_path)


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


# ============================================================
# 真实服务商客户端骨架(以阿里云为例; 腾讯/讯飞同理继承 AsrProvider)
# ============================================================


class AliyunAsrProvider(AsrProvider):
    """阿里云智能语音交互(录音文件识别 + 说话人分离)客户端骨架。

    P1 已搭好结构: 凭证读取 + 参数校验 + 统一返回 AsrSegment。
    接入时只需补 _call_api + _parse_response 两段真实实现。

    阿里云录音文件识别(带说话人分离)接入要点:
    1. 依赖: 阿里云 DashScope SDK(`dashscope`)或 REST API;
    2. 凭证: AccessKeyId / AccessKeySecret(settings.asr_api_key / asr_api_secret);
    3. 能力: 录音文件识别(paraformer-v2)支持 speaker_count 参数做说话人分离;
    4. 返回: 每个句子含 speaker_id / begin_time / end_time / text, 映射到 AsrSegment。

    参照: https://help.aliyun.com/zh/model-studio/ (DashScope 语音识别)。
    """

    name = "aliyun"

    def transcribe(self, audio_path: str) -> List[AsrSegment]:
        try:
            from config.settings import settings
        except Exception:  # noqa: BLE001
            settings = None

        api_key = getattr(settings, "asr_api_key", "") if settings else ""
        api_secret = getattr(settings, "asr_api_secret", "") if settings else ""
        if not api_key or not api_secret:
            raise RuntimeError(
                "阿里云 ASR 未配置凭证(asr_api_key / asr_api_secret), "
                "请在 config/.env 填入后再调用。"
            )

        # TODO(P1 接入): 调用阿里云录音文件识别 API(带 speaker_count 说话人分离)
        #   raw = self._call_api(audio_path, api_key, api_secret)
        #   return self._parse_response(raw)
        raise NotImplementedError(
            "阿里云 ASR 客户端骨架已就绪, 尚未接入真实网络调用(需 DashScope SDK/凭证)。"
            "详见 AliyunAsrProvider.transcribe 内注释的接入要点。"
        )

    def _call_api(self, audio_path: str, api_key: str, api_secret: str) -> Dict:
        """调用阿里云录音文件识别 API(真实实现待接入)。"""
        raise NotImplementedError("待接入: 阿里云录音文件识别 HTTP 调用")

    def _parse_response(self, raw: Dict) -> List[AsrSegment]:
        """把阿里云响应(含句子/说话人/时间戳)解析为 AsrSegment 列表。"""
        segments: List[AsrSegment] = []
        # TODO(P1 接入): 遍历 raw["sentences"], 每条映射:
        #   speaker  <- sentence["speaker_id"]  (或 speaker 编号)
        #   text     <- sentence["text"]
        #   start_ms <- sentence["begin_time"]
        #   end_ms   <- sentence["end_time"]
        #   confidence <- sentence.get("confidence", 1.0)
        return segments


# 注册真实服务商骨架(骨架本身未接入网络, 调用会抛 NotImplementedError/RuntimeError,
# 但已进入注册表, 保证 transcribe(provider="aliyun") 走统一分派路径)。
register_provider(AliyunAsrProvider())
