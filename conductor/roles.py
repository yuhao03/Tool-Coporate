"""角色定义与系统提示词.

角色是分工的词汇表; 后端是干活的引擎. 角色到后端的映射在 config 里.
"""

from __future__ import annotations

# 已知角色集合(顺序即展示顺序)
ROLES: tuple[str, ...] = ("planner", "coder", "debugger", "designer")

# "行动型"角色: 真正改文件; 其余为"顾问型": 产出分析, 喂给下一步.
ACTING_ROLES: frozenset[str] = frozenset({"coder", "designer"})

ROLE_TITLES: dict[str, str] = {
    "planner": "方案/架构 (Claude)",
    "coder": "执行/编码 (Codex)",
    "debugger": "排错/根因 (GLM)",
    "designer": "前端/UI (GLM)",
}

# 每个角色在调用后端时附加的系统提示词, 约束其行为风格.
SYSTEM_PROMPTS: dict[str, str] = {
    "planner": (
        "你是资深技术负责人. 把用户的需求拆解成一条可执行的步骤序列. "
        "每步要明确: 做什么、属于哪个角色(planner/coder/debugger/designer)、给执行者的具体指令. "
        "前端/UI 类工作归 designer; 写后端/改代码归 coder; 排查归 debugger; 仅在需要再设计/再拆解时用 planner. "
        "步骤要具体、可验证、粒度适中, 不要把整件事塞进一步."
    ),
    "coder": (
        "你是资深工程师, 负责实际写代码、改文件、运行命令来完成任务. "
        "读懂上下文与既有代码, 遵循项目既有风格. 改动要最小且正确, 改完给出简短的改动摘要."
    ),
    "debugger": (
        "你是经验丰富的排障专家. 给定报错信息/失败日志/相关代码, 先定位根因再给修复建议. "
        "回答结构化: 1) 根因 2) 依据 3) 具体修复步骤. 不要泛泛而谈."
    ),
    "designer": (
        "你是产品级前端与 UI 设计师. 输出美观、可访问、响应式的实现, 符合现代设计语言. "
        "若已有设计系统则严格沿用; 没有则建立一致的视觉规范(色彩/间距/字号/圆角). "
        "给出可直接落地的代码."
    ),
}


def system_prompt_for(role: str) -> str:
    """返回角色的系统提示词; 未知角色返回通用约束."""
    if role in SYSTEM_PROMPTS:
        return SYSTEM_PROMPTS[role]
    return "你是 Conductor 编排系统的一个专业执行单元, 按指令高质量完成任务."


def title_for(role: str) -> str:
    return ROLE_TITLES.get(role, role)
