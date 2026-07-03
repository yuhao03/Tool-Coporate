"""成本统计: token 用量与价格估算.

定价表为占位默认值(USD / 每百万 token, (输入, 输出)), 可被 config 的 [cost] 覆盖.
真实价格以各厂商官网为准; 模型名做模糊匹配.
"""

from __future__ import annotations

from dataclasses import dataclass

# 每百万 token 价格 (input_usd, output_usd). 仅作估算默认值.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # 智谱 GLM (占位估算, 实际以 bigmodel.cn 官方价目为准)
    "glm-5.2": (0.6, 2.2),
    "glm-5": (0.6, 2.2),
    "glm-4.6": (0.6, 2.2),
    "glm-4.5": (2.0, 8.0),
    "glm-4.5-air": (0.5, 0.5),
    # Anthropic Claude
    "claude-fable-5": (3.0, 15.0),
    "claude-mythos-5": (5.0, 25.0),
    "claude-opus-4-8": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # OpenAI / Codex(默认模型族)
    "gpt-5": (5.0, 15.0),
    "gpt-5-codex": (5.0, 15.0),
    "gpt-4.1": (2.0, 8.0),
    "o3": (10.0, 40.0),
}


@dataclass
class Usage:
    """单次调用的 token 用量."""

    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "model": self.model}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Usage | None":
        if not d:
            return None
        return cls(
            input_tokens=int(d.get("input_tokens", 0) or 0),
            output_tokens=int(d.get("output_tokens", 0) or 0),
            model=d.get("model"),
        )


def _match_key(model: str, table: dict[str, tuple[float, float]]) -> str | None:
    m = (model or "").lower().strip()
    if not m:
        return None
    if m in table:
        return m
    # 前缀/包含模糊匹配(如 "claude-fable-5-20260101")
    for k in table:
        if m.startswith(k) or k in m:
            return k
    return None


def estimate_cost_usd(
    usage: Usage | None,
    pricing: dict[str, tuple[float, float]] | None = None,
) -> float | None:
    """按用量与定价估算 USD 成本; 信息不足返回 None."""
    if usage is None or usage.total_tokens == 0:
        return None
    table = pricing or DEFAULT_PRICING
    key = _match_key(usage.model or "", table)
    if not key:
        return None
    ip, op = table[key]
    return round(usage.input_tokens / 1_000_000 * ip + usage.output_tokens / 1_000_000 * op, 6)


def format_cost(cost_usd: float | None) -> str:
    if cost_usd is None:
        return "—"
    if cost_usd < 0.01:
        return f"${cost_usd*1000:.3f}m"  # 毫美元
    return f"${cost_usd:.4f}"
