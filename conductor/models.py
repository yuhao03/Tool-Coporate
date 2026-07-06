"""模型预设: 供终端模型选择器(conductor model / --set)使用。"""

from __future__ import annotations

# 按后端类型分组的常见模型(仅作选择建议, 可自定义输入任意 id)
MODEL_PRESETS: dict[str, list[str]] = {
    "claude-cli": [
        "claude-opus-4.7", "claude-opus-4.6", "claude-sonnet-5",
        "claude-fable-5", "claude-haiku-4-5",
    ],
    "codex-cli": ["gpt-5", "gpt-5-codex", "gpt-4.1", "o3"],
    "openai-compatible": ["glm-5.2", "glm-5-turbo", "glm-4.6", "gpt-5", "gpt-4.1"],
}


def presets_for(backend_type: str) -> list[str]:
    """返回该后端类型适合的候选模型列表。"""
    return list(MODEL_PRESETS.get(backend_type, []))
