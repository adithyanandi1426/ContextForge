from __future__ import annotations

from dataclasses import dataclass

from .models import Dependency, EdgeType, GraphNode, NodeType, RepositoryGraph


@dataclass(frozen=True)
class UpgradeImpact:
    dependency: Dependency
    target_version: str
    modules: tuple[GraphNode, ...]
    files: tuple[GraphNode, ...]
    confidence: str
    caveat: str


def analyze_dependency_upgrade(
    graph: RepositoryGraph,
    group: str,
    artifact: str,
    target_version: str,
) -> list[UpgradeImpact]:
    """Report observed dependency consumers without asserting API breakage."""
    target_dependency_ids = {
        node.id
        for node in graph.nodes.values()
        if node.type == NodeType.DEPENDENCY
        and node.name == f"{group}:{artifact}"
    }
    direct_dependencies = [
        dependency
        for dependency in graph.dependencies
        if dependency.group == group and dependency.artifact == artifact
    ]
    results = []
    for dependency in direct_dependencies:
        for dependency_id in target_dependency_ids:
            consumers = {
                edge.source
                for edge in graph.edges
                if edge.target == dependency_id
                and edge.type == EdgeType.REFERENCES
            }
            modules = {
                edge.source
                for edge in graph.edges
                if edge.target in consumers
                and edge.type == EdgeType.CONTAINS
                and edge.source in graph.nodes
                and graph.nodes[edge.source].type == NodeType.MODULE
            }
            results.append(UpgradeImpact(
                dependency=dependency,
                target_version=target_version,
                modules=tuple(
                    graph.nodes[node_id]
                    for node_id in sorted(modules)
                ),
                files=tuple(
                    graph.nodes[node_id]
                    for node_id in sorted(consumers)
                    if node_id in graph.nodes
                ),
                confidence="inferred",
                caveat=(
                    "Consumers were inferred from Java import package prefixes "
                    "matching the Maven group. This identifies review scope, not "
                    "binary/source incompatibility."
                ),
            ))
    return results
