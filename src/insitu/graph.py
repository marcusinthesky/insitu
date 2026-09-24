"""Small cycle-checked dependency graph."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable
from graphlib import CycleError, TopologicalSorter
from typing import Generic, TypeVar

Node = TypeVar("Node", bound=Hashable)


class DependencyCycleError(ValueError):
    """Raised when an edge would make the graph cyclic."""

    def __init__(self, cycle: tuple[Hashable, ...]) -> None:
        self.cycle = cycle
        super().__init__("dependency cycle: " + " -> ".join(map(str, cycle)))


class Dag(Generic[Node]):
    """Directed acyclic graph where each node stores its dependencies."""

    def __init__(self) -> None:
        self._dependencies: dict[Node, set[Node]] = defaultdict(set)

    def add_node(self, node: Node) -> None:
        """Add a node with no dependencies."""
        self._dependencies.setdefault(node, set())

    def add_dependency(self, node: Node, dependency: Node) -> None:
        """Add ``dependency -> node`` and reject cycles immediately."""
        self._dependencies.setdefault(node, set())
        self._dependencies.setdefault(dependency, set())
        if dependency in self._dependencies[node]:
            return
        path = self._path(dependency, node)
        if path is not None:
            raise DependencyCycleError((node, *path))
        self._dependencies[node].add(dependency)

    def dependencies(self, node: Node) -> frozenset[Node]:
        """Return direct dependencies for ``node``."""
        return frozenset(self._dependencies.get(node, ()))

    def order(self) -> tuple[Node, ...]:
        """Return a stable topological order."""
        sorter = TopologicalSorter(self._dependencies)
        try:
            sorter.prepare()
        except CycleError as error:
            raw = error.args[1] if len(error.args) > 1 else ()
            raise DependencyCycleError(tuple(raw)) from error
        ready: list[Node] = []
        while sorter.is_active():
            batch = sorted(sorter.get_ready(), key=str)
            ready.extend(batch)
            sorter.done(*batch)
        return tuple(ready)

    def affected(self, changed: set[Node]) -> frozenset[Node]:
        """Return changed nodes and all transitive dependents."""
        reverse: dict[Node, set[Node]] = defaultdict(set)
        for node, dependencies in self._dependencies.items():
            for dependency in dependencies:
                reverse[dependency].add(node)
        queue = deque(changed)
        found = set(changed)
        while queue:
            for dependent in reverse[queue.popleft()]:
                if dependent not in found:
                    found.add(dependent)
                    queue.append(dependent)
        return frozenset(found)

    def _path(self, start: Node, target: Node) -> tuple[Node, ...] | None:
        """Return one dependency path from ``start`` to ``target``."""
        queue: deque[tuple[Node, tuple[Node, ...]]] = deque([(start, (start,))])
        seen: set[Node] = set()
        while queue:
            node, path = queue.popleft()
            if node == target:
                return path
            if node in seen:
                continue
            seen.add(node)
            queue.extend(
                (dependency, (*path, dependency))
                for dependency in sorted(self._dependencies[node], key=str)
                if dependency not in seen
            )
        return None
