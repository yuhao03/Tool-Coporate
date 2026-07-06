"""路由: planner meta-prompt 与 JSON 计划解析.

planner(Claude) 收到任务, 输出结构化 JSON 计划; 本模块负责构造该请求并解析结果.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .roles import ROLES, system_prompt_for

PLANNER_ROLE = "planner"


@dataclass
class Step:
    title: str
    role: str
    instruction: str
    depends_on: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 角色合法性兜底: 非法角色一律视为 coder
        if self.role not in ROLES:
            self.role = "coder"


def build_plan_prompt(task: str, context: str = "") -> str:
    """构造给 planner 的用户消息(要求严格 JSON 输出)."""
    ctx_block = f"\n\n[补充上下文]\n{context}" if context else ""
    return (
        f"任务:\n{task}{ctx_block}\n\n"
        "请把该任务拆成一条可执行步骤序列, 严格输出下面的 JSON 结构"
        "(只输出 JSON 本身, 不要 markdown 代码围栏, 不要任何解释):\n"
        "{\n"
        '  "steps": [\n'
        '    {"title": "简短标题", "role": "coder|designer|debugger|planner", '
        '"instruction": "给执行者的具体指令", "depends_on": ["前置步骤标题, 无则空数组"]}\n'
        "  ]\n"
        "}\n"
        "规则:\n"
        "- role 仅限 planner / coder / debugger / designer.\n"
        "- 写后端代码/改文件/跑命令 → coder; 前端与 UI → designer; 排查问题 → debugger.\n"
        "- 步骤数 2~6 步, 粒度适中, 每步可独立验证.\n"
        "- 顺序应自然: 通常先 designer/coder 实现, 必要时再 debugger 验证.\n"
        "- depends_on 引用前置步骤的 title; 无依赖用空数组 [].\n"
    )


def parse_plan(text: str) -> list[Step] | None:
    """从 planner 输出解析步骤列表. 解析失败返回 None(交由上层降级)."""
    if not text:
        return None
    raw = _extract_json(text)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    steps_raw = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps_raw, list) or not steps_raw:
        return None
    steps: list[Step] = []
    for s in steps_raw:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title", "")).strip()
        role = str(s.get("role", "coder")).strip()
        instruction = str(s.get("instruction", "")).strip()
        if not (title or instruction):
            continue
        depends = s.get("depends_on") or []
        depends = [str(x) for x in depends] if isinstance(depends, list) else []
        steps.append(Step(
            title=title or instruction[:40],
            role=role,
            instruction=instruction or title,
            depends_on=depends,
        ))
    return steps or None


def _extract_json(text: str) -> str | None:
    """从可能含 markdown 围栏或解释文字的输出中抠出最外层 JSON 对象."""
    t = text.strip()
    # 去 ```json ... ``` / ``` ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
    if fence:
        return fence.group(1)
    # 取第一个 '{' 到最后一个 '}'
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start : end + 1]
    return None


def system_prompt() -> str:
    return system_prompt_for(PLANNER_ROLE)


# --------------------------------------------------------------------------- #
# 开发闭环 (dev loop): 规划(claude) → 执行(codex) → 审核(glm) → 重规划(claude)
# --------------------------------------------------------------------------- #
def build_exec_doc_prompt(task: str, context: str = "") -> str:
    """让 planner 产出一份「执行文档」供 Codex 照做。"""
    ctx = f"\n\n[补充上下文]\n{context}" if context else ""
    return (
        f"任务:\n{task}{ctx}\n\n"
        "请作为资深技术负责人, 产出一分简洁的「执行文档」供 Codex 直接照做:\n"
        "- 要改/新增哪些文件, 各自做什么\n"
        "- 关键实现思路、接口/函数签名约定\n"
        "- 验收标准(怎样算做对了)\n"
        "只输出 markdown 文档本身, 不要寒暄。"
    )


def build_review_prompt(task: str, doc: str, diff: str, exec_summary: str,
                        verify_out: str) -> str:
    """让 GLM 审核本轮实现, 输出结构化 JSON。"""
    diff_block = (f"\n[本轮代码改动 diff]\n{diff}" if diff
                  else f"\n[Codex 执行摘要]\n{exec_summary[:2000]}" if exec_summary else "")
    verify_block = f"\n[校验输出]\n{verify_out[:2000]}" if verify_out else ""
    return (
        f"你是严格的代码审核专家。判断本轮实现是否「正确实现了任务」。\n\n"
        f"[任务]\n{task}\n\n[执行文档]\n{doc[:3000]}"
        f"{diff_block}{verify_block}\n\n"
        "只输出如下 JSON(不要 markdown 围栏):\n"
        '{"approved": true/false, '
        '"bugs": ["具体问题(含位置+现象+修复建议)", "..."], '
        '"summary": "一句话总评"}\n'
        "规则: 已正确实现且无明显 bug → approved=true; 否则 approved=false 并在 bugs 列出每个问题。"
    )


def parse_review(text: str) -> dict | None:
    """解析 GLM 审核输出。返回 {approved, bugs, summary} 或 None。"""
    if not text:
        return None
    raw = _extract_json(text)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    approved = bool(data.get("approved", False))
    bugs = data.get("bugs", [])
    if not isinstance(bugs, list):
        bugs = [str(bugs)]
    else:
        bugs = [str(b) for b in bugs if b]
    return {"approved": approved, "bugs": bugs,
            "summary": str(data.get("summary", "")).strip()}


def build_replan_prompt(task: str, prev_doc: str, review: dict, verify_out: str) -> str:
    """让 planner 基于审核问题重新出执行文档。"""
    bugs = review.get("bugs", []) if review else []
    bugs_block = "\n".join(f"- {b}" for b in bugs) or "- (审核未给出具体问题)"
    verify_block = f"\n[校验输出]\n{verify_out[:2000]}" if verify_out else ""
    return (
        f"任务:\n{task}\n\n"
        f"[上一版执行文档]\n{prev_doc[:3000]}\n\n"
        f"[GLM 审核未通过, 发现的问题]\n{bugs_block}{verify_block}\n\n"
        "请针对上述问题, 重新产出一分「修订执行文档」供 Codex 照做, 务必解决这些问题。\n"
        "只输出 markdown 文档本身。"
    )

