from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import EdgeType, RepositoryGraph


@dataclass(frozen=True)
class CompiledContext:
    question: str
    files: tuple[str, ...]
    text: str
    reason_by_file: dict[str, str]


def compile_context(
    graph: RepositoryGraph,
    question: str,
    *,
    token_budget: int,
    count_tokens: Callable[[str], int],
) -> CompiledContext:
    """Rank relevant source files and include complete files within a token cap."""
    if token_budget <= 0:
        raise ValueError("token_budget must be greater than zero.")

    terms = {
        term.lower()
        for term in question.replace("/", " ").replace(".", " ").split()
        if len(term) > 2
    }
    scores: dict[str, tuple[int, str]] = {}
    for path, record in graph.files.items():
        haystack = (path + "\n" + record.content).lower()
        matched_terms = sorted(term for term in terms if term in haystack)
        if matched_terms:
            scores[path] = (
                len(matched_terms),
                "Matched question terms: " + ", ".join(matched_terms),
            )

    direct_matches = set(scores)
    for edge in graph.edges:
        if edge.type not in {
            EdgeType.IMPORTS, EdgeType.REFERENCES, EdgeType.CALLS,
            EdgeType.EXTENDS, EdgeType.IMPLEMENTS,
        }:
            continue
        source_node = graph.nodes.get(edge.source)
        target_node = graph.nodes.get(edge.target)
        if source_node is None or target_node is None:
            continue
        source_path = source_node.path
        target_path = target_node.path
        if source_path in direct_matches and target_path in graph.files:
            related_path = target_path
            related_symbol = source_node.name
        elif target_path in direct_matches and source_path in graph.files:
            related_path = source_path
            related_symbol = target_node.name
        else:
            continue
        if related_path in direct_matches:
            continue
        prior = scores.get(related_path, (0, ""))
        scores[related_path] = (
            prior[0] + 1,
            prior[1] or f"Related through a Java relationship to {related_symbol}",
        )

    ranked = sorted(scores, key=lambda path: (-scores[path][0], path))
    header = f"Task: {question.strip()}\n\n"
    used = count_tokens(header)
    if used > token_budget:
        raise ValueError("The task description exceeds the context token budget.")
    selected: list[str] = []
    reasons: dict[str, str] = {}
    chunks = [header]
    for path in ranked:
        record = graph.files[path]
        chunk = f"--- {path} ---\n{record.content.rstrip()}\n\n"
        chunk_tokens = count_tokens(chunk)
        if used + chunk_tokens > token_budget:
            continue
        selected.append(path)
        reasons[path] = scores[path][1]
        chunks.append(chunk)
        used += chunk_tokens
    return CompiledContext(
        question=question,
        files=tuple(selected),
        text="".join(chunks),
        reason_by_file=reasons,
    )
