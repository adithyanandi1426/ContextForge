from __future__ import annotations

import hashlib
from collections import defaultdict

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from ..models import Edge, EdgeType, Evidence, FileRecord, GraphNode, NodeType


def stable_id(*parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def node_text(source: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


class JavaAnalyzer:
    language = "java"
    extensions = frozenset({".java"})
    version = "tree-sitter-java-v1"

    def analyze(
        self,
        record: FileRecord,
    ) -> tuple[list[GraphNode], list[Edge], list[str]]:
        source = record.content.encode("utf-8")
        tree = get_parser("java").parse(source)
        file_id = f"file:{record.path}"
        nodes = [
            GraphNode(
                id=file_id,
                type=NodeType.TEST if "/test/" in f"/{record.path.lower()}/" else NodeType.FILE,
                name=record.path.rsplit("/", 1)[-1],
                path=record.path,
                attributes={"language": self.language, "sha256": record.sha256},
            )
        ]
        edges = []
        limitations = []
        package_name = ""
        declarations: dict[str, list[GraphNode]] = defaultdict(list)
        unresolved_imports: list[tuple[str, int]] = []

        def visit(
            node: Node,
            parent_id: str | None = None,
        ) -> None:
            nonlocal package_name
            if node.type == "package_declaration":
                package_name = node_text(source, node).removeprefix("package").strip().rstrip(";")
            elif node.type == "import_declaration":
                import_name = (
                    node_text(source, node)
                    .removeprefix("import")
                    .strip()
                    .rstrip(";")
                    .removeprefix("static ")
                )
                unresolved_imports.append((import_name, node.start_point.row + 1))
            elif node.type in {
                "class_declaration", "interface_declaration", "enum_declaration",
                "record_declaration", "annotation_type_declaration",
            }:
                name = node_text(source, node.child_by_field_name("name"))
                kind = (
                    NodeType.INTERFACE
                    if node.type in {"interface_declaration", "annotation_type_declaration"}
                    else NodeType.CLASS
                )
                enclosing_type = (
                    parent_id.rsplit(":", 1)[-1]
                    if parent_id and parent_id.startswith(f"class:{record.path}:")
                    else ""
                )
                qualified_name = (
                    f"{enclosing_type}.{name}"
                    if enclosing_type
                    else f"{package_name}.{name}" if package_name else name
                )
                class_id = f"class:{record.path}:{qualified_name}"
                class_node = GraphNode(
                    id=class_id,
                    type=kind,
                    name=qualified_name,
                    path=record.path,
                    line=node.start_point.row + 1,
                    attributes={"simple_name": name},
                )
                nodes.append(class_node)
                declarations[name].append(class_node)
                edges.append(Edge(
                    source=file_id,
                    target=class_id,
                    type=EdgeType.CONTAINS,
                    evidence=Evidence(
                        record.path,
                        node.start_point.row + 1,
                        self.version,
                        1.0,
                        f"Java AST declaration: {node.type}",
                    ),
                ))
                if parent_id is not None:
                    edges.append(Edge(
                        source=parent_id,
                        target=class_id,
                        type=EdgeType.CONTAINS,
                        evidence=Evidence(record.path, node.start_point.row + 1, self.version, 1.0, "Nested Java type"),
                    ))
                visit_class_children(node, class_id, qualified_name)
                return
            for child in node.named_children:
                visit(child, parent_id)

        def visit_class_children(
            class_node: Node,
            class_id: str,
            qualified_name: str,
        ) -> None:
            for child in class_node.named_children:
                if child.type == "superclass":
                    type_node = child.named_children[-1] if child.named_children else None
                    parent_name = node_text(source, type_node).split(".")[-1]
                    edges.append(Edge(
                        source=class_id,
                        target=f"unresolved-type:{parent_name}",
                        type=EdgeType.EXTENDS,
                        evidence=Evidence(
                            record.path,
                            child.start_point.row + 1,
                            self.version,
                            0.7,
                            f"Java AST superclass declaration '{parent_name}'; target not resolved",
                        ),
                    ))
                elif child.type == "super_interfaces":
                    for type_node in child.named_children:
                        interface_name = node_text(source, type_node).split(".")[-1]
                        edges.append(Edge(
                            source=class_id,
                            target=f"unresolved-type:{interface_name}",
                            type=EdgeType.IMPLEMENTS,
                            evidence=Evidence(
                                record.path,
                                type_node.start_point.row + 1,
                                self.version,
                                0.7,
                                f"Java AST interface declaration '{interface_name}'; target not resolved",
                            ),
                        ))
            body = next(
                (
                    child for child in class_node.named_children
                    if child.type == "class_body"
                ),
                None,
            )
            if body is None:
                return
            for child in body.named_children:
                if child.type in {
                    "method_declaration",
                    "constructor_declaration",
                }:
                    method_name = node_text(
                        source,
                        child.child_by_field_name("name"),
                    )
                    method_id = (
                        f"method:{record.path}:{qualified_name}:"
                        f"{method_name}:{child.start_point.row + 1}"
                    )
                    method_node = GraphNode(
                        id=method_id,
                        type=NodeType.METHOD,
                        name=method_name,
                        path=record.path,
                        line=child.start_point.row + 1,
                        attributes={"owner": qualified_name},
                    )
                    nodes.append(method_node)
                    declarations[method_name].append(method_node)
                    edges.append(Edge(
                        source=class_id,
                        target=method_id,
                        type=EdgeType.CONTAINS,
                        evidence=Evidence(
                            record.path,
                            child.start_point.row + 1,
                            self.version,
                            1.0,
                            f"Java AST declaration: {child.type}",
                        ),
                    ))
                    for descendant in descendants(child):
                        if descendant.type == "method_invocation":
                            called_name = node_text(
                                source,
                                descendant.child_by_field_name("name"),
                            )
                            edges.append(Edge(
                                source=method_id,
                                target=f"unresolved-method:{called_name}",
                                type=EdgeType.REFERENCES,
                                evidence=Evidence(
                                    record.path,
                                    descendant.start_point.row + 1,
                                    self.version,
                                    0.65,
                                    f"Syntactic method invocation '{called_name}'; target not compiler-resolved",
                                ),
                            ))
                        elif descendant.type == "object_creation_expression":
                            type_node = descendant.child_by_field_name("type")
                            type_name = node_text(source, type_node).split(".")[-1]
                            edges.append(Edge(
                                source=method_id,
                                target=f"unresolved-type:{type_name}",
                                type=EdgeType.REFERENCES,
                                evidence=Evidence(
                                    record.path,
                                    descendant.start_point.row + 1,
                                    self.version,
                                    0.65,
                                    f"Syntactic object creation '{type_name}'; target not compiler-resolved",
                                ),
                            ))
                elif child.type in {
                    "class_declaration", "interface_declaration", "enum_declaration",
                    "record_declaration", "annotation_type_declaration",
                }:
                    visit(child, class_id)

        def descendants(node: Node):
            stack = list(reversed(node.named_children))
            while stack:
                current = stack.pop()
                yield current
                stack.extend(reversed(current.named_children))

        visit(tree.root_node)

        for import_path, import_line in unresolved_imports:
            imported_name = import_path.rsplit(".", 1)[-1]
            local_matches = declarations.get(imported_name, [])
            if local_matches:
                imported_node = local_matches[0]
                edges.append(Edge(
                    source=file_id,
                    target=imported_node.id,
                    type=EdgeType.IMPORTS,
                    evidence=Evidence(
                        record.path,
                        import_line,
                        self.version,
                        0.9,
                        f"Import '{import_path}' matches an indexed type by simple name",
                    ),
                ))
            else:
                edges.append(Edge(
                    source=file_id,
                    target=f"external-import:{import_path}",
                    type=EdgeType.IMPORTS,
                    evidence=Evidence(
                        record.path,
                        import_line,
                        self.version,
                        0.9,
                        f"Java AST import '{import_path}'",
                    ),
                ))

        if tree.root_node.has_error:
            limitations.append(
                f"Java parser reported syntax errors in {record.path}; extracted relationships may be incomplete."
            )
        if any(edge.target.startswith(("unresolved-method:", "unresolved-type:")) for edge in edges):
            limitations.append(
                f"Some Java references in {record.path} could not be resolved within the indexed source set."
            )
        return nodes, edges, limitations


def descendants(node: Node):
    stack = list(reversed(node.named_children))
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))
