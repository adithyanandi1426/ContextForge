from __future__ import annotations

from ..models import FileRecord


class TextFallbackAnalyzer:
    language = "text"
    extensions = frozenset()
    version = "text-fallback-v1"

    def analyze(self, record: FileRecord):
        return [], [], [
            f"No structural analyzer is registered for {record.language} files."
        ]
