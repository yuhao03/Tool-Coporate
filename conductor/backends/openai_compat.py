"""OpenAI 兼容后端: 智谱 GLM / OpenAI / DeepSeek 等任何兼容服务.

智谱 BigModel: base_url=https://open.bigmodel.cn/api/paas/v4/, model=glm-5.2, Bearer 认证.
"""

from __future__ import annotations

import logging

import httpx

from .base import Backend, BackendRequest, BackendResult

log = logging.getLogger("conductor.backend.http")


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
            return BackendResult(ok=True, text=(text or "").strip(), model=model, meta=meta)
        except (ValueError, KeyError, IndexError) as e:
            return BackendResult(
                ok=False, text="", error=f"解析响应失败: {e}; 原始: {resp.text[:300]}",
                meta=meta,
            )

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
