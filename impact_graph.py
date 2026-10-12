from __future__ import annotations

import re
from collections import defaultdict, deque

import plotly.graph_objects as go


IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_]\w*")
CLASS_PATTERN = re.compile(
    r"^\s*(?:export\s+|public\s+|internal\s+|static\s+|"
    r"pub(?:\([^)]*\))?\s+|abstract\s+|final\s+|data\s+|sealed\s+|"
    r"open\s+|partial\s+)*"
    r"(class|interface|enum|record|struct|trait|type)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
FUNCTION_PATTERNS = (
    re.compile(
        r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
        r"function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
        r"fn\s+([A-Za-z_]\w*)\s*[<(]",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:(?:public|protected|private|internal|static|final|"
        r"abstract|virtual|override|synchronized|async|suspend|extern|"
        r"inline|default)\s+)+[\w<>,?.\[\]:*&]+\s+"
        r"([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:throws\b[^{]+)?(?:\{|$)",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|"
        r"open\s+|override\s+|suspend\s+)*(?:inline\s+)?"
        r"fun\s+([A-Za-z_]\w*)\s*\(",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:async\s+)?([A-Za-z_$]\w*)\s*\([^;{}]*\)\s*\{",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:(?:virtual|static|inline|constexpr|explicit)\s+)*"
        r"[\w:<>*&]+\s+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*"
        r"(?:const\s*)?(?:noexcept\s*)?\{",
        re.MULTILINE,
    ),
)
COMMON_SYMBOLS = {
    "get", "set", "main", "init", "new", "toString", "equals", "hashCode",
    "close", "run", "build", "test", "if", "for", "while", "switch",
    "catch", "with", "match",
}
NODE_LIMIT = 100
RELATION_LIMIT = 250


def extract_definitions(content: str) -> list[tuple[str, str, int]]:
    definitions: set[tuple[str, str, int]] = set()
    for match in CLASS_PATTERN.finditer(content):
        line = content.count("\n", 0, match.start()) + 1
        definitions.add((match.group(2), "class", line))

    for pattern in FUNCTION_PATTERNS:
        for match in pattern.finditer(content):
            name = match.group(1)
            if name not in COMMON_SYMBOLS:
                line = content.count("\n", 0, match.start()) + 1
                definitions.add((name, "method/function", line))
    return sorted(definitions, key=lambda entry: (entry[2], entry[0]))


def definitions_for(file: dict) -> list[tuple[str, str, int]]:
    if "definitions" in file:
        return file["definitions"]
    return extract_definitions(file["content"])


def find_impact_targets(files: list[dict], query: str) -> list[str]:
    query = query.strip().casefold()
    if not query:
        return []

    query_terms = {
        term for term in re.findall(r"[A-Za-z_]\w*", query)
        if len(term) >= 3
        and term not in {
            "the", "and", "for", "how", "what", "where", "show", "find",
            "trace", "impact", "impacted", "module", "class", "method",
            "file", "files", "from", "with", "this", "that",
        }
    }
    matches = []
    for file in files:
        path = file["path"].casefold()
        definitions = definitions_for(file)
        symbol_names = {name.casefold() for name, _, _ in definitions}
        if query in symbol_names:
            rank = (0, 0)
        elif path.rsplit("/", 1)[-1].split(".", 1)[0] == query:
            rank = (0, 1)
        elif query in path:
            rank = (0, 2)
        else:
            path_matches = sum(term in path for term in query_terms)
            symbol_matches = sum(
                any(term in name for name in symbol_names)
                for term in query_terms
            )
            relevance = path_matches + 2 * symbol_matches
            if not query_terms or relevance == 0:
                continue
            rank = (1, -relevance)
        matches.append((rank, file["path"]))
    return [path for _, path in sorted(matches)[:50]]


def build_impact_graph(
    files: list[dict],
    target_path: str,
    depth: int = 2,
) -> tuple[go.Figure, dict]:
    definitions_by_name: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    definitions_by_path: dict[str, list[tuple[str, str, int]]] = {}
    for file in files:
        definitions = definitions_for(file)
        definitions_by_path[file["path"]] = definitions
        for name, kind, line in definitions:
            if name not in COMMON_SYMBOLS:
                providers = definitions_by_name[name]
                if not any(path == file["path"] for path, _, _ in providers):
                    providers.append((file["path"], kind, line))

    relations = []
    ambiguous_names = {
        name for name, providers in definitions_by_name.items()
        if len(providers) > 1
    }
    for file in files:
        content = file["content"]
        identifiers: dict[str, int] = {}
        for line_number, line in enumerate(content.splitlines(), start=1):
            for name in IDENTIFIER_PATTERN.findall(line):
                identifiers.setdefault(name, line_number)

        for name in identifiers.keys() & definitions_by_name.keys():
            providers = definitions_by_name[name]
            if name in ambiguous_names:
                continue
            provider_path, kind, definition_line = providers[0]
            if provider_path == file["path"]:
                continue
            is_call = re.search(rf"\b{re.escape(name)}\s*\(", content)
            is_ruby_reference = (
                file["path"].casefold().endswith(".rb")
                and name in identifiers
            )
            if (
                kind == "method/function"
                and not is_call
                and not is_ruby_reference
            ):
                continue
            relations.append({
                "caller": file["path"],
                "provider": provider_path,
                "symbol": name,
                "kind": kind,
                "reference_line": identifiers[name],
                "definition_line": definition_line,
            })

    dependencies: dict[str, set[str]] = defaultdict(set)
    dependents: dict[str, set[str]] = defaultdict(set)
    for relation in relations:
        dependencies[relation["caller"]].add(relation["provider"])
        dependents[relation["provider"]].add(relation["caller"])

    def walk(graph: dict[str, set[str]]) -> dict[str, int]:
        distances = {target_path: 0}
        queue = deque([target_path])
        while queue:
            current = queue.popleft()
            if distances[current] >= depth:
                continue
            for neighbor in sorted(graph.get(current, ())):
                if neighbor not in distances:
                    distances[neighbor] = distances[current] + 1
                    queue.append(neighbor)
        return distances

    impacted_distances = walk(dependents)
    dependency_distances = walk(dependencies)
    impacted_distances.pop(target_path, None)
    dependency_distances.pop(target_path, None)

    file_levels = {target_path: 0}
    for path, distance in dependency_distances.items():
        file_levels[path] = -distance
    for path, distance in impacted_distances.items():
        existing = file_levels.get(path)
        if existing is None or distance < abs(existing):
            file_levels[path] = distance

    selected_paths = set(file_levels)
    visible_relations = [
        relation for relation in relations
        if relation["caller"] in selected_paths
        and relation["provider"] in selected_paths
    ]
    truncated = (
        len(selected_paths) > NODE_LIMIT
        or len(visible_relations) > RELATION_LIMIT
    )
    if len(selected_paths) > NODE_LIMIT:
        nearest = sorted(
            selected_paths,
            key=lambda path: (abs(file_levels[path]), path),
        )[:NODE_LIMIT]
        selected_paths = set(nearest)
        file_levels = {
            path: level for path, level in file_levels.items()
            if path in selected_paths
        }
        visible_relations = [
            relation for relation in visible_relations
            if relation["caller"] in selected_paths
            and relation["provider"] in selected_paths
        ]
    if len(visible_relations) > RELATION_LIMIT:
        visible_relations = visible_relations[:RELATION_LIMIT]

    file_nodes = sorted(selected_paths)
    positions: dict[str, tuple[float, float]] = {}
    labels: dict[str, str] = {}
    hover: dict[str, str] = {}
    colors: dict[str, str] = {}

    levels: dict[int, list[str]] = defaultdict(list)
    for path in file_nodes:
        levels[file_levels[path]].append(path)
    for level, paths in levels.items():
        for index, path in enumerate(sorted(paths)):
            node_id = f"file:{path}"
            positions[node_id] = (float(level), (len(paths) - 1) / 2 - index)
            labels[node_id] = path.rsplit("/", 1)[-1]
            hover[node_id] = f"File: {path}"
            if path == target_path:
                colors[node_id] = "#e45756"
            elif path in impacted_distances:
                colors[node_id] = "#f2a541"
            else:
                colors[node_id] = "#4c9f70"

    unique_symbols = sorted({
        relation["symbol"] for relation in visible_relations
    })
    for index, name in enumerate(unique_symbols):
        node_id = f"symbol:{name}"
        symbol_relations = [
            relation for relation in visible_relations
            if relation["symbol"] == name
        ]
        provider_level = file_levels[symbol_relations[0]["provider"]]
        caller_levels = [
            file_levels[relation["caller"]] for relation in symbol_relations
        ]
        symbol_x = (
            provider_level + sum(caller_levels) / len(caller_levels)
        ) / 2
        positions[node_id] = (
            symbol_x,
            (len(unique_symbols) - 1) / 2 - index,
        )
        symbol_kinds = sorted({
            relation["kind"] for relation in visible_relations
            if relation["symbol"] == name
        })
        labels[node_id] = name
        provider = symbol_relations[0]
        callers = sorted({
            f"{relation['caller']}:{relation['reference_line']}"
            for relation in symbol_relations
        })
        hover[node_id] = (
            f"{', '.join(symbol_kinds)}: {name}<br>"
            f"Defined at {provider['provider']}:"
            f"{provider['definition_line']}<br>"
            "Referenced at:<br>" + "<br>".join(callers)
        )
        colors[node_id] = "#748ffc"

    edges = []
    for relation in visible_relations:
        symbol_id = f"symbol:{relation['symbol']}"
        caller_id = f"file:{relation['caller']}"
        provider_id = f"file:{relation['provider']}"
        edges.append((caller_id, symbol_id, relation))
        edges.append((symbol_id, provider_id, relation))

    edge_x: list[float | None] = []
    edge_y: list[float | None] = []
    for start, end, _ in edges:
        edge_x.extend([positions[start][0], positions[end][0], None])
        edge_y.extend([positions[start][1], positions[end][1], None])

    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=edge_x,
        y=edge_y,
        mode="lines",
        line={"width": 1, "color": "#9aa0a6"},
        hoverinfo="skip",
        showlegend=False,
    ))
    for start, end, _ in edges:
        figure.add_annotation(
            x=positions[end][0],
            y=positions[end][1],
            ax=positions[start][0],
            ay=positions[start][1],
            xref="x",
            yref="y",
            axref="x",
            ayref="y",
            text="",
            showarrow=True,
            arrowhead=2,
            arrowsize=1,
            arrowwidth=1,
            arrowcolor="#9aa0a6",
        )
    figure.add_trace(go.Scatter(
        x=[positions[node][0] for node in positions],
        y=[positions[node][1] for node in positions],
        text=[labels[node] for node in positions],
        customdata=[hover[node] for node in positions],
        mode="markers+text",
        textposition="middle right",
        marker={
            "size": [
                25 if node == f"file:{target_path}" else 17
                for node in positions
            ],
            "color": [colors[node] for node in positions],
            "line": {"width": 1, "color": "white"},
        },
        hovertemplate="%{customdata}<extra></extra>",
        showlegend=False,
    ))
    figure.update_layout(
        title="Dependency and impact graph",
        height=max(500, min(900, 25 * len(positions))),
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
        xaxis={
            "title": "Dependencies  ←  target module  →  impacted callers",
            "showticklabels": False,
            "zeroline": True,
            "zerolinecolor": "#cccccc",
            "showgrid": False,
        },
        yaxis={"visible": False, "showgrid": False},
        hovermode="closest",
    )
    figure.add_annotation(
        x=0.01, y=1.04, xref="paper", yref="paper",
        text="Red: target · Orange: impacted callers · Green: dependencies · Blue: class/method",
        showarrow=False, xanchor="left",
    )

    direct_impact = sorted(dependents.get(target_path, ()))
    transitive_impact = sorted(impacted_distances)
    return figure, {
        "target": target_path,
        "direct_impact": direct_impact,
        "transitive_impact": transitive_impact,
        "impacted_details": [
            {
                "file": path,
                "depth": impacted_distances[path],
                "definitions": [
                    f"{kind} {name} (line {line})"
                    for name, kind, line in definitions_by_path[path]
                ],
            }
            for path in transitive_impact
        ],
        "relationships": [
            {
                "caller": relation["caller"],
                "reference_line": relation["reference_line"],
                "symbol": relation["symbol"],
                "provider": relation["provider"],
                "definition_line": relation["definition_line"],
            }
            for relation in visible_relations
        ],
        "dependencies": sorted(dependencies.get(target_path, ())),
        "symbols": sorted({
            relation["symbol"] for relation in visible_relations
        }),
        "files_shown": len(selected_paths),
        "relationships_shown": len(visible_relations),
        "truncated": truncated,
        "ambiguous_symbols_omitted": len(ambiguous_names),
        "evidence_note": (
            "Static name-reference analysis. A matching identifier/call is "
            "evidence of a possible dependency, not compiler-resolved "
            "proof; dynamic dispatch, reflection, generated code, and "
            "language-specific resolution may be missed."
        ),
    }
