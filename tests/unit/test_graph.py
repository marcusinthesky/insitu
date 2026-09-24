"""Dependency graph tests."""

import pytest

from insitu.graph import Dag, DependencyCycleError

pytestmark = pytest.mark.unit


def test_order_and_affected() -> None:
    graph: Dag[str] = Dag()
    graph.add_dependency("render", "query")
    graph.add_dependency("query", "source")
    assert graph.order() == ("source", "query", "render")
    assert graph.affected({"source"}) == {"source", "query", "render"}


def test_cycle_reports_path() -> None:
    graph: Dag[str] = Dag()
    graph.add_dependency("a", "b")
    with pytest.raises(DependencyCycleError, match="b -> a -> b"):
        graph.add_dependency("b", "a")
