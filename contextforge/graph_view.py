from __future__ import annotations

from collections import defaultdict, deque

import plotly.graph_objects as go

from .models import EdgeType, RepositoryGraph


VISIBLE_EDGE_TYPES = {
    EdgeType.CONTAINS,
    EdgeType.IMPORTS,
    EdgeType.REFERENCES,
    EdgeType.CALLS,
    EdgeType.EXTENDS,
    EdgeType.IMPLEMENTS,
}


def build_graph_figure(
    graph: RepositoryGraph,
    roots: set[str],
    *,
    max_depth: int = 3,
    max_nodes: int = 120,
) -> go.Figure:
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in graph.edges:
        if edge.type not in VISIBLE_EDGE_TYPES:
            continue
        adjacency[edge.source].append((edge.target, edge.type.value))
        adjacency[edge.target].append((edge.source, edge.type.value))

    depth_by_node = {node_id: 0 for node_id in roots if node_id in graph.nodes}
    queue = deque(depth_by_node)
    selected_edges: set[tuple[str, str, str]] = set()
    while queue and len(depth_by_node) < max_nodes:
        node_id = queue.popleft()
        depth = depth_by_node[node_id]
        if depth >= max_depth:
            continue
        for neighbor, edge_type in sorted(adjacency.get(node_id, [])):
            selected_edges.add((node_id, neighbor, edge_type))
            if neighbor in graph.nodes and neighbor not in depth_by_node:
                depth_by_node[neighbor] = depth + 1
                queue.append(neighbor)
                if len(depth_by_node) >= max_nodes:
                    break

    layers: dict[int, list[str]] = defaultdict(list)
    for node_id, depth in depth_by_node.items():
        layers[depth].append(node_id)
    positions = {}
    layer_count = max(layers, default=0)
    for depth, node_ids in layers.items():
        node_ids.sort(key=lambda item: graph.nodes[item].name.casefold())
        for index, node_id in enumerate(node_ids):
            positions[node_id] = (
                float(depth),
                index - (len(node_ids) - 1) / 2,
            )

    figure = go.Figure()
    for source, target, edge_type in selected_edges:
        if source not in positions or target not in positions:
            continue
        x0, y0 = positions[source]
        x1, y1 = positions[target]
        figure.add_trace(go.Scatter(
            x=[x0, x1, None],
            y=[y0, y1, None],
            mode="lines",
            line={"width": 1, "color": "#94a3b8"},
            hoverinfo="skip",
            showlegend=False,
        ))
        figure.add_annotation(
            x=(x0 + x1) / 2,
            y=(y0 + y1) / 2,
            text=edge_type.lower(),
            showarrow=False,
            font={"size": 8, "color": "#475569"},
        )

    node_ids = list(positions)
    figure.add_trace(go.Scatter(
        x=[positions[node_id][0] for node_id in node_ids],
        y=[positions[node_id][1] for node_id in node_ids],
        mode="markers+text",
        text=[
            graph.nodes[node_id].name[:42]
            for node_id in node_ids
        ],
        textposition="top center",
        customdata=[
            [
                graph.nodes[node_id].type.value,
                graph.nodes[node_id].path or "",
                graph.nodes[node_id].line or "",
            ]
            for node_id in node_ids
        ],
        hovertemplate=(
            "<b>%{text}</b><br>Type: %{customdata[0]}"
            "<br>Path: %{customdata[1]}<br>Line: %{customdata[2]}"
            "<extra></extra>"
        ),
        marker={
            "size": 18,
            "color": [
                depth_by_node[node_id]
                for node_id in node_ids
            ],
            "colorscale": "Blues",
            "showscale": False,
            "line": {"width": 1, "color": "#1e293b"},
        },
        showlegend=False,
    ))
    figure.update_layout(
        height=max(420, 150 + 65 * max((len(items) for items in layers.values()), default=1)),
        margin={"l": 20, "r": 20, "t": 35, "b": 20},
        xaxis={
            "visible": False,
            "range": [-0.5, layer_count + 0.6],
        },
        yaxis={"visible": False},
        hovermode="closest",
        title="Evidence-linked repository impact graph",
    )
    return figure
