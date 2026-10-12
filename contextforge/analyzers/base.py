from __future__ import annotations

from typing import Protocol

from ..models import Edge, FileRecord, GraphNode


class LanguageAnalyzer(Protocol):
    language: str
    extensions: frozenset[str]
    version: str

    def analyze(
        self,
        record: FileRecord,
    ) -> tuple[list[GraphNode], list[Edge], list[str]]:
        """Return stable nodes, evidence-bearing edges, and limitations."""
