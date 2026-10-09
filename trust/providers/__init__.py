"""模型 judge provider；导入本包不会触发网络或要求 API key。"""

from trust.providers.base import JudgeProvider
from trust.providers.openai_provider import OpenAIProvider

__all__ = ["JudgeProvider", "OpenAIProvider"]

