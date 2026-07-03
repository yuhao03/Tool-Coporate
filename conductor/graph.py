"""依赖图与并发调度.

把 planner 产出的 steps(带 depends_on) 组织成 DAG, 检测环, 然后按"波次"执行:
同一波次内无相互依赖的步骤可并发(受 max_workers 限制), 下一波等当前波完成.

注意: 并发仅对"无依赖"步骤安全. 若多个 acting 步骤(coder/designer)并发改同一
工作目录, 可能产生文件冲突 —— 故 max_workers 默认 1(串行), 需要并发时由调用方显式开启.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from .router import Step

log = logging.getLogger("conductor.graph")


@dataclass
class Node:
    title: str
    step: Step
    deps: set[str] = field(default_factory=set)


class CycleError(ValueError):
    pass


def build_graph(steps: list[Step]) -> dict[str, Node]:
    """构建 title->Node. 未知依赖(指向不存在步骤)会被忽略并记录."""
    known = {s.title for s in steps}
    graph: dict[str, Node] = {}
    for s in steps:
        deps = {d for d in s.depends_on if d in known}
        ignored = [d for d in s.depends_on if d not in known]
        if ignored:
            log.warning("步骤 %r 的依赖 %s 不存在, 已忽略", s.title, ignored)
        graph[s.title] = Node(title=s.title, step=s, deps=deps)
    return graph


def detect_cycle(graph: dict[str, Node]) -> bool:
    """DFS 三色标记检测环."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {t: WHITE for t in graph}

    def visit(t: str) -> bool:
        color[t] = GRAY
        for d in graph[t].deps:
            if color.get(d) == GRAY:
                return True
            if color.get(d, BLACK) == WHITE and visit(d):
                return True
        color[t] = BLACK
        return False

    return any(color[t] == WHITE and visit(t) for t in graph)


def topological_waves(graph: dict[str, Node]) -> list[list[str]]:
    """返回波次列表, 每波是可并发的步骤标题. 有环抛 CycleError."""
    if detect_cycle(graph):
        raise CycleError("依赖图存在环, 无法调度")
    remaining = dict(graph)
    done: set[str] = set()
    waves: list[list[str]] = []
    while remaining:
        ready = sorted(t for t, n in remaining.items() if n.deps <= done)
        if not ready:
            raise CycleError("依赖无法满足(可能存在环或孤立依赖)")
        waves.append(ready)
        done |= set(ready)
        for t in ready:
            del remaining[t]
    return waves


# 执行函数签名: (step, {依赖标题: 其结果}) -> 该步骤的结果(任意类型)
ExecFn = Callable[[Step, dict[str, Any]], Any]


class Scheduler:
    """波次化并发调度器."""

    def __init__(self, steps: list[Step], max_workers: int = 1) -> None:
        self.steps = steps
        self.max_workers = max(1, max_workers)
        self.graph = build_graph(steps)

    def run(self, exec_fn: ExecFn) -> dict[str, Any]:
        """执行所有步骤, 返回 {标题: 结果}. 同一波次内并发."""
        results: dict[str, Any] = {}
        waves = topological_waves(self.graph)
        for wave in waves:
            if len(wave) == 1 or self.max_workers == 1:
                for t in wave:
                    results[t] = exec_fn(self.graph[t].step, self._ctx(self.graph[t], results))
            else:
                with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                    fut = {
                        pool.submit(
                            exec_fn, self.graph[t].step, self._ctx(self.graph[t], results)
                        ): t
                        for t in wave
                    }
                    for f in as_completed(fut):
                        results[fut[f]] = f.result()
        return results

    @staticmethod
    def _ctx(node: Node, results: dict[str, Any]) -> dict[str, Any]:
        return {d: results[d] for d in node.deps if d in results}

    def waves_preview(self) -> list[list[str]]:
        return topological_waves(self.graph)
