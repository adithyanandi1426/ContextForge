from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class NodeType(str, Enum):
    REPOSITORY = "repository"
    MODULE = "module"
    FILE = "file"
    CLASS = "class"
    INTERFACE = "interface"
    METHOD = "method"
    FUNCTION = "function"
    DEPENDENCY = "dependency"
    TEST = "test"
    CONFIGURATION = "configuration"


class EdgeType(str, Enum):
    CONTAINS = "CONTAINS"
    IMPORTS = "IMPORTS"
    DEPENDS_ON = "DEPENDS_ON"
    CALLS = "CALLS"
    IMPLEMENTS = "IMPLEMENTS"
    EXTENDS = "EXTENDS"
    TESTS = "TESTS"
    CONFIGURES = "CONFIGURES"
    REFERENCES = "REFERENCES"


@dataclass(frozen=True)
class Evidence:
    source_path: str
    line: int | None
    method: str
    confidence: float
    detail: str


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: NodeType
    name: str
    path: str | None = None
    line: int | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    type: EdgeType
    evidence: Evidence


@dataclass(frozen=True)
class Dependency:
    group: str
    artifact: str
    version: str | None
    scope: str | None
    manifest: str
    line: int | None = None

    @property
    def coordinate(self) -> str:
        return f"{self.group}:{self.artifact}"


@dataclass
class FileRecord:
    path: str
    language: str
    content: str
    sha256: str
    size: int


@dataclass
class RepositoryGraph:
    root: Path
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    files: dict[str, FileRecord] = field(default_factory=dict)
    dependencies: list[Dependency] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
