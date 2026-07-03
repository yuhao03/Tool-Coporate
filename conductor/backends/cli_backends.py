"""CLI 后端: 复用本机已认证的 claude / codex CLI(无头模式)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..cost import Usage
from .base import Backend, BackendRequest, BackendResult

log = logging.getLogger("conductor.backend.cli")


def _parse_claude_json(out: str):
    """解析 claude --output-format json 的输出.

    返回 (text, usage, model, cost_usd); 解析失败返回 (None, None, None, None).
    典型结构: {"result":"...", "usage":{"input_tokens":..,"output_tokens":..},
              "total_cost_usd":.., "model":"..."}
    """
    try:
        data = json.loads(out)
    except (ValueError, json.JSONDecodeError):
        return None, None, None, None
    text = data.get("result")
    raw_usage = data.get("usage") or {}
    usage = None
    if raw_usage:
        usage = Usage(
            input_tokens=int(raw_usage.get("input_tokens") or 0),
            output_tokens=int(raw_usage.get("output_tokens") or 0),
            model=data.get("model"),
        )
    cost = data.get("total_cost_usd")
    if cost is not None:
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            cost = None
    return text, usage, data.get("model"), cost


def _run(
    cmd: list[str], cwd: Path | None, timeout: int, input_text: str | None = None
) -> tuple[int, str, str, str | None]:
    """运行子进程, 返回 (returncode, stdout, stderr, timeout_msg).

    使用参数列表(非 shell 字符串), 跨平台一致且无需转义.
    """
    try:
        kwargs: dict = dict(
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if input_text is not None:
            kwargs["input"] = input_text
        else:
            # 显式关闭 stdin, 防止子进程在非交互/无 tty 环境下挂起等待输入.
            kwargs["stdin"] = subprocess.DEVNULL
        proc = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        return 124, "", "", f"超时({timeout}s)"
    except FileNotFoundError:
        return 127, "", "", "可执行文件不存在: " + cmd[0]
    return proc.returncode, proc.stdout or "", proc.stderr or "", None


class ClaudeCliBackend(Backend):
    """`claude -p` 无头调用. 复用 ~/.claude 现有认证, 无需 API key."""

    kind = "claude-cli"

    def _bin(self) -> str:
        return self.cfg.executable or "claude"

    def complete(self, req: BackendRequest) -> BackendResult:
        bin_ = self._bin()
        # --output-format json: 拿到结构化结果 + usage + 成本
        cmd: list[str] = [bin_, "-p", req.prompt, "--output-format", "json"]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        cmd += list(self.cfg.extra_args)
        timeout = req.timeout or self.cfg.timeout
        code, out, err, tmsg = _run(cmd, req.cwd, timeout)
        meta = {"cmd": _truncate_cmd(cmd)}
        if tmsg:
            return BackendResult(ok=False, text="", error=tmsg, meta=meta)
        if code != 0:
            detail = (err or out or "").strip()
            return BackendResult(ok=False, text=out, error=detail[:500] or f"退出码 {code}", meta=meta)
        text, usage, model, cost = _parse_claude_json(out)
        if text is None:  # JSON 解析失败, 退回原始文本
            text = out.strip()
            model = self.cfg.model or "claude"
        return BackendResult(ok=True, text=text, model=model or "claude",
                             usage=usage, cost_usd=cost, meta=meta)

    def health(self) -> tuple[bool, str]:
        path = shutil.which(self._bin())
        if path:
            return True, f"claude CLI 就绪: {path}"
        return False, "未找到 claude CLI (安装 Claude Code 后重试)"

    def describe(self, req: BackendRequest) -> str:
        cmd = _truncate_cmd([self._bin(), "-p", req.prompt])
        return f"[{self.name}] {cmd}"


class CodexCliBackend(Backend):
    """`codex exec` 无头调用. 复用 ~/.codex 现有认证, 无需 API key."""

    kind = "codex-cli"

    def _bin(self) -> str:
        return self.cfg.executable or "codex"

    def _build_cmd(self, req: BackendRequest, out_file: str) -> list[str]:
        cmd: list[str] = [self._bin(), "exec"]
        # 全自动: 绕过审批/沙箱(在本机自己的仓库里执行, 风险可控);
        # 否则退到 workspace-write 沙箱.
        if self.cfg.full_auto:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            cmd += ["-s", "workspace-write"]
        if req.cwd:
            cmd += ["-C", str(req.cwd)]
        cmd += ["--skip-git-repo-check", "--color", "never"]
        if self.cfg.model:
            cmd += ["-m", self.cfg.model]
        for k, v in self.cfg.extra_config.items():
            cmd += ["-c", f"{k}={v}"]
        cmd += list(self.cfg.extra_args)
        # 干净捕获最终消息: 写到临时文件, 避免进度日志混入 stdout.
        cmd += ["-o", out_file]
        cmd.append(req.prompt)
        return cmd

    def complete(self, req: BackendRequest) -> BackendResult:
        timeout = req.timeout or self.cfg.timeout
        with tempfile.NamedTemporaryFile(
            "w+", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            out_file = tmp.name
        cmd = self._build_cmd(req, out_file)
        code, _out, err, tmsg = _run(cmd, req.cwd, timeout)
        meta = {"cmd": _truncate_cmd(cmd)}
        text = ""
        try:
            text = Path(out_file).read_text(encoding="utf-8").strip()
        except OSError:
            pass
        finally:
            try:
                Path(out_file).unlink()
            except OSError:
                pass
        if tmsg:
            return BackendResult(ok=False, text=text, error=tmsg, meta=meta)
        if code != 0 and not text:
            return BackendResult(ok=False, text="", error=err or f"退出码 {code}", meta=meta)
        # 有最终消息即视为成功(codex 可能用非零退出码表达"已尽力")
        ok = bool(text)
        return BackendResult(
            ok=ok, text=text, model=self.cfg.model or "codex",
            error=None if ok else (err or f"退出码 {code}"), meta=meta,
        )

    def health(self) -> tuple[bool, str]:
        path = shutil.which(self._bin())
        if path:
            return True, f"codex CLI 就绪: {path}"
        return False, "未找到 codex CLI (安装 Codex CLI 后重试)"

    def describe(self, req: BackendRequest) -> str:
        cmd = _truncate_cmd([self._bin(), "exec", "<prompt>"] + (
            ["--dangerously-bypass-approvals-and-sandbox"] if self.cfg.full_auto else []
        ))
        return f"[{self.name}] {cmd}"


def _truncate_cmd(cmd: list[str], n: int = 160) -> str:
    """把命令列表拼成可读字符串, 长提示词截断."""
    parts = []
    for p in cmd:
        if len(p) > 120:
            p = p[:120] + "…"
        parts.append(p)
    s = " ".join(parts)
    return s if len(s) <= n else s[:n] + " …"
