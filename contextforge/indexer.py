from __future__ import annotations

import hashlib
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from .analyzers import JavaAnalyzer, TextFallbackAnalyzer
from .models import (
    Dependency,
    Edge,
    EdgeType,
    Evidence,
    FileRecord,
    GraphNode,
    NodeType,
    RepositoryGraph,
)


SOURCE_EXTENSIONS = {
    ".java", ".kt", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".go", ".cs", ".cpp", ".h", ".hpp", ".rs", ".rb",
}
LANGUAGE_BY_EXTENSION = {
    ".java": "java", ".kt": "kotlin", ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".cs": "csharp",
    ".cpp": "cpp", ".h": "cpp", ".hpp": "cpp", ".rs": "rust",
    ".rb": "ruby",
}
SKIP_DIRECTORIES = {
    ".git", ".idea", ".vscode", "node_modules", "vendor", "target",
    "build", "dist", "out", ".gradle", "__pycache__", ".venv",
    "venv", "coverage",
}
MAX_FILE_BYTES = 350_000
MAX_FILES = 20_000


def stable_node_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]


class RepositoryIndexer:
    def __init__(self) -> None:
        self._java = JavaAnalyzer()
        self._fallback = TextFallbackAnalyzer()

    def index(self, root: Path) -> RepositoryGraph:
        root = root.resolve()
        if not root.is_dir():
            raise ValueError(f"Repository root is not a directory: {root}")

        graph = RepositoryGraph(root=root)
        repository_id = f"repository:{stable_node_id(str(root))}"
        graph.add_node(GraphNode(
            id=repository_id,
            type=NodeType.REPOSITORY,
            name=root.name,
            path=".",
        ))

        source_paths = self._source_paths(root)
        if len(source_paths) >= MAX_FILES:
            graph.limitations.append(
                f"Source scan capped at {MAX_FILES} files."
            )

        for path in source_paths:
            relative = path.relative_to(root).as_posix()
            try:
                content_bytes = path.read_bytes()
            except OSError as exc:
                graph.limitations.append(
                    f"Could not read {relative}: {type(exc).__name__}."
                )
                continue
            content = content_bytes.decode("utf-8", errors="replace")
            record = FileRecord(
                path=relative,
                language=LANGUAGE_BY_EXTENSION[path.suffix.lower()],
                content=content,
                sha256=hashlib.sha256(content_bytes).hexdigest(),
                size=len(content_bytes),
            )
            graph.files[relative] = record
            graph.add_node(GraphNode(
                id=f"file:{relative}",
                type=NodeType.TEST if "/test/" in f"/{relative.lower()}/" else NodeType.FILE,
                name=path.name,
                path=relative,
                attributes={"language": record.language, "sha256": record.sha256},
            ))
            graph.add_edge(Edge(
                source=repository_id,
                target=f"file:{relative}",
                type=EdgeType.CONTAINS,
                evidence=Evidence(
                    relative,
                    None,
                    "file-scanner-v1",
                    1.0,
                    f"Indexed {record.language} source file",
                ),
            ))
            analyzer = self._java if record.language == "java" else self._fallback
            nodes, edges, limitations = analyzer.analyze(record)
            graph.limitations.extend(limitations)
            for node in nodes:
                graph.add_node(node)
            graph.edges.extend(edges)

        self._resolve_java_references(graph)
        self._index_maven(graph, repository_id)
        graph.limitations = list(dict.fromkeys(graph.limitations))
        return graph

    def _source_paths(self, root: Path) -> list[Path]:
        paths = []
        for directory, subdirectories, filenames in os.walk(root):
            subdirectories[:] = sorted(
                name for name in subdirectories
                if name not in SKIP_DIRECTORIES
            )
            for filename in filenames:
                path = Path(directory) / filename
                if path.suffix.lower() not in SOURCE_EXTENSIONS:
                    continue
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                paths.append(path)
                if len(paths) >= MAX_FILES:
                    return sorted(paths)
        return sorted(paths)

    def _resolve_java_references(self, graph: RepositoryGraph) -> None:
        types_by_name: dict[str, list[GraphNode]] = {}
        methods_by_name: dict[str, list[GraphNode]] = {}
        for node in graph.nodes.values():
            if node.type in {NodeType.CLASS, NodeType.INTERFACE}:
                simple_name = node.attributes.get(
                    "simple_name",
                    node.name.rsplit(".", 1)[-1],
                )
                types_by_name.setdefault(simple_name, []).append(node)
            elif node.type == NodeType.METHOD:
                methods_by_name.setdefault(node.name, []).append(node)

        resolved_edges = []
        for edge in graph.edges:
            prefixes = ("unresolved-type:", "external-import:")
            match_prefix = next(
                (prefix for prefix in prefixes if edge.target.startswith(prefix)),
                None,
            )
            if match_prefix:
                name = edge.target.removeprefix(match_prefix)
                simple_name = name.rsplit(".", 1)[-1]
                candidates = (
                    [
                        node for node in types_by_name.get(simple_name, [])
                        if node.name == name
                    ]
                    if match_prefix == "external-import:"
                    else types_by_name.get(simple_name, [])
                )
                if len(candidates) == 1:
                    graph.add_node(candidates[0])
                    resolved_edges.append(Edge(
                        source=edge.source,
                        target=candidates[0].id,
                        type=edge.type,
                        evidence=Evidence(
                            edge.evidence.source_path,
                            edge.evidence.line,
                            edge.evidence.method,
                            0.9,
                            edge.evidence.detail
                            + "; uniquely matched in indexed Java declarations",
                        ),
                    ))
                    continue
            if edge.target.startswith("unresolved-method:"):
                name = edge.target.removeprefix("unresolved-method:")
                candidates = methods_by_name.get(name, [])
                if len(candidates) == 1:
                    resolved_edges.append(Edge(
                        source=edge.source,
                        target=candidates[0].id,
                        type=EdgeType.REFERENCES,
                        evidence=Evidence(
                            edge.evidence.source_path,
                            edge.evidence.line,
                            edge.evidence.method,
                            0.75,
                            edge.evidence.detail
                            + "; unique method name in indexed Java files, "
                            "not compiler-resolved",
                        ),
                    ))
                    continue
            resolved_edges.append(edge)
        graph.edges = resolved_edges

    def _index_maven(
        self,
        graph: RepositoryGraph,
        repository_id: str,
    ) -> None:
        external_imports_by_file: dict[str, list[Edge]] = {}
        for edge in graph.edges:
            if (
                edge.type == EdgeType.IMPORTS
                and edge.target.startswith("external-import:")
            ):
                external_imports_by_file.setdefault(edge.source, []).append(edge)

        manifest_paths = []
        for directory, subdirectories, filenames in os.walk(graph.root):
            subdirectories[:] = sorted(
                name for name in subdirectories
                if name not in SKIP_DIRECTORIES
            )
            if "pom.xml" in filenames:
                manifest_paths.append(Path(directory) / "pom.xml")
        for manifest in sorted(manifest_paths):
            relative_manifest = manifest.relative_to(graph.root).as_posix()
            try:
                root_element = ET.parse(manifest).getroot()
            except (ET.ParseError, OSError) as exc:
                graph.limitations.append(
                    f"Could not parse Maven manifest {relative_manifest}: "
                    f"{type(exc).__name__}."
                )
                continue

            namespace = ""
            if root_element.tag.startswith("{"):
                namespace = root_element.tag.split("}", 1)[0] + "}"

            def find_text(element: ET.Element, name: str) -> str | None:
                child = element.find(f"{namespace}{name}")
                return child.text.strip() if child is not None and child.text else None

            property_values = {}
            properties = root_element.find(f"{namespace}properties")
            if properties is not None:
                property_values = {
                    child.tag.removeprefix(namespace): (child.text or "").strip()
                    for child in properties
                }
            module_path = Path(relative_manifest).parent.as_posix()
            module_id = f"module:{stable_node_id(module_path)}"
            module_name = find_text(root_element, "artifactId") or (
                graph.root.name if module_path == "." else module_path.rsplit("/", 1)[-1]
            )
            graph.add_node(GraphNode(
                id=module_id,
                type=NodeType.MODULE,
                name=module_name,
                path=module_path,
            ))
            graph.add_edge(Edge(
                source=repository_id,
                target=module_id,
                type=EdgeType.CONTAINS,
                evidence=Evidence(
                    relative_manifest,
                    None,
                    "maven-xml-v1",
                    1.0,
                    "Maven project descriptor identifies a module",
                ),
            ))
            config_id = f"configuration:{stable_node_id(relative_manifest)}"
            graph.add_node(GraphNode(
                id=config_id,
                type=NodeType.CONFIGURATION,
                name="pom.xml",
                path=relative_manifest,
            ))
            graph.add_edge(Edge(
                source=module_id,
                target=config_id,
                type=EdgeType.CONTAINS,
                evidence=Evidence(relative_manifest, None, "maven-xml-v1", 1.0, "Maven build configuration"),
            ))

            dependencies = root_element.find(f"{namespace}dependencies")
            if dependencies is None:
                continue
            for element in dependencies.findall(f"{namespace}dependency"):
                group = find_text(element, "groupId")
                artifact = find_text(element, "artifactId")
                version = find_text(element, "version")
                scope = find_text(element, "scope")
                if not group or not artifact:
                    graph.limitations.append(
                        f"Incomplete dependency declaration in {relative_manifest}."
                    )
                    continue
                if version and version.startswith("${") and version.endswith("}"):
                    property_name = version[2:-1]
                    resolved_version = property_values.get(property_name)
                    if resolved_version is None:
                        graph.limitations.append(
                            f"Unresolved Maven version property {version} "
                            f"in {relative_manifest}."
                        )
                    else:
                        version = resolved_version
                if version is None:
                    graph.limitations.append(
                        f"Maven dependency {group}:{artifact} has no explicit "
                        f"version in {relative_manifest}; managed version is "
                        "not resolved."
                    )
                dependency = Dependency(
                    group=group,
                    artifact=artifact,
                    version=version,
                    scope=scope,
                    manifest=relative_manifest,
                )
                graph.dependencies.append(dependency)
                dependency_id = f"dependency:{stable_node_id(group, artifact)}"
                graph.add_node(GraphNode(
                    id=dependency_id,
                    type=NodeType.DEPENDENCY,
                    name=dependency.coordinate,
                    path=relative_manifest,
                    attributes={
                        "version": version or "",
                        "scope": scope or "",
                    },
                ))
                graph.add_edge(Edge(
                    source=module_id,
                    target=dependency_id,
                    type=EdgeType.DEPENDS_ON,
                    evidence=Evidence(
                        relative_manifest,
                        None,
                        "maven-xml-v1",
                        1.0,
                        f"Declared Maven dependency {dependency.coordinate}"
                        + (f":{version}" if version else ""),
                    ),
                ))
                package_prefix = group.replace("-", ".")
                for file_id, imports in external_imports_by_file.items():
                    matching_import = next(
                        (
                            edge for edge in imports
                            if edge.target.removeprefix(
                                "external-import:"
                            ).startswith(package_prefix)
                        ),
                        None,
                    )
                    if matching_import is not None:
                        file_path = file_id.removeprefix("file:")
                        graph.add_edge(Edge(
                            source=file_id,
                            target=dependency_id,
                            type=EdgeType.REFERENCES,
                            evidence=Evidence(
                                file_path,
                                matching_import.evidence.line,
                                "maven-coordinate-package-prefix-v1",
                                0.55,
                                f"Imported package resembles dependency group {group}; "
                                "coordinate usage is inferred, not resolved",
                            ),
                        ))

        modules = sorted([
            node for node in graph.nodes.values()
            if node.type == NodeType.MODULE
        ], key=lambda node: len((node.path or ".").split("/")), reverse=True)
        for file_path in graph.files:
            owning_module = next(
                (
                    module for module in modules
                    if module.path == "."
                    or file_path.startswith((module.path or ".") + "/")
                ),
                None,
            )
            if owning_module is not None:
                graph.add_edge(Edge(
                    source=owning_module.id,
                    target=f"file:{file_path}",
                    type=EdgeType.CONTAINS,
                    evidence=Evidence(
                        file_path,
                        None,
                        "maven-module-path-v1",
                        1.0,
                        "Source file resides beneath Maven module directory",
                    ),
                ))
