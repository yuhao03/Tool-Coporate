"""OpenAI 兼容后端: 智谱 GLM / OpenAI / DeepSeek 等任何兼容服务.

智谱 BigModel: base_url=https://open.bigmodel.cn/api/paas/v4/, model=glm-5.2, Bearer 认证.
"""

from __future__ import annotations

import json
import logging

import httpx

from ..cost import Usage
from .base import Backend, BackendRequest, BackendResult, StreamEvent

log = logging.getLogger("conductor.backend.http")


def _usage_from(raw: dict | None, model: str | None) -> Usage | None:
    """把 OpenAI 风格 usage(prompt_tokens/completion_tokens) 转成 Usage."""
    if not raw:
        return None
    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
        model=model,
    )


class OpenAICompatibleBackend(Backend):
    kind = "openai-compatible"

    def _url(self) -> str:
        base = self.cfg.base_url.rstrip("/") + "/"
        return base + "chat/completions"

    def _messages(self, req: BackendRequest) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        if req.system:
            msgs.append({"role": "system", "content": req.system})
        msgs.append({"role": "user", "content": req.prompt})
        return msgs

    def complete(self, req: BackendRequest) -> BackendResult:
        key = self.cfg.resolved_api_key
        meta: dict = {"url": self._url(), "model": self.cfg.model}
        if not key:
            env_hint = f" (环境变量 {self.cfg.api_key_env} 未设置)" if self.cfg.api_key_env else ""
            return BackendResult(ok=False, text="", error=f"缺少 API key{env_hint}", meta=meta)
        payload: dict = {
            "model": self.cfg.model,
            "messages": self._messages(req),
        }
        if self.cfg.temperature is not None:
            payload["temperature"] = self.cfg.temperature
        if req.json_mode:
            payload["response_format"] = {"type": "json_object"}
        timeout = req.timeout or self.cfg.timeout
        try:
            resp = httpx.post(
                self._url(),
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        except httpx.HTTPError as e:  # 网络/连接错误
            return BackendResult(ok=False, text="", error=f"请求失败: {e}", meta=meta)
        if resp.status_code >= 400:
            body = resp.text
            if len(body) > 500:
                body = body[:500] + " …"
            return BackendResult(
                ok=False, text="", error=f"HTTP {resp.status_code}: {body}", meta=meta
            )
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            model = data.get("model", self.cfg.model)
            usage = _usage_from(data.get("usage"), model)
            return BackendResult(
                ok=True, text=(text or "").strip(), model=model, usage=usage, meta=meta)
        except (ValueError, KeyError, IndexError) as e:
            return BackendResult(
                ok=False, text="", error=f"解析响应失败: {e}; 原始: {resp.text[:300]}",
                meta=meta,
            )

    def stream(self, req: BackendRequest):
        """SSE 真流式: 边接收边产出 delta, 最后给出带 usage 的 done."""
        key = self.cfg.resolved_api_key
        if not key:
            yield StreamEvent(type="error", error="缺少 API key")
            return
        payload: dict = {"model": self.cfg.model, "messages": self._messages(req),
                         "stream": True}
        if self.cfg.temperature is not None:
            payload["temperature"] = self.cfg.temperature
        if req.json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload["stream_options"] = {"include_usage": True}
        timeout = req.timeout or self.cfg.timeout
        usage: Usage | None = None
        try:
            with httpx.stream("POST", self._url(), json=payload,
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              timeout=timeout) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", "ignore")[:500]
                    yield StreamEvent(type="error", error=f"HTTP {resp.status_code}: {body}")
                    return
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usage"):
                        usage = _usage_from(obj["usage"], self.cfg.model)
                    choices = obj.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield StreamEvent(type="delta", text=content)
        except httpx.HTTPError as e:
            yield StreamEvent(type="error", error=f"流式请求失败: {e}")
            return
        yield StreamEvent(type="done", text="", usage=usage, model=self.cfg.model)

    def health(self) -> tuple[bool, str]:
        key = self.cfg.resolved_api_key
        if not key:
            env = f"环境变量 {self.cfg.api_key_env}" if self.cfg.api_key_env else "api_key"
            return False, f"未配置密钥 ({env})"
        return True, f"已配置密钥, base_url={self.cfg.base_url or '(未设置)'}"

    def describe(self, req: BackendRequest) -> str:
        prompt = req.prompt
        if len(prompt) > 120:
            prompt = prompt[:120] + " …"
        return f"[{self.name}] POST {self._url()} model={self.cfg.model} | {prompt}"
