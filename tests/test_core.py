from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from contextforge.context_compiler import compile_context
from contextforge.indexer import RepositoryIndexer
from contextforge.models import EdgeType, NodeType
from contextforge.upgrade import analyze_dependency_upgrade


class RepositoryCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "pom.xml").write_text(
            """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <properties><client.version>1.2.0</client.version></properties>
  <dependencies>
    <dependency>
      <groupId>org.example.client</groupId>
      <artifactId>client-api</artifactId>
      <version>${client.version}</version>
    </dependency>
  </dependencies>
</project>
""",
            encoding="utf-8",
        )
        java_directory = (
            self.root / "src" / "main" / "java" / "org" / "example" / "app"
        )
        java_directory.mkdir(parents=True)
        (java_directory / "UseClient.java").write_text(
            """package org.example.app;
import org.example.client.Client;
public class UseClient {
    public void send() {
        new Client().execute();
    }
    class Nested { void helper() {} }
}
""",
            encoding="utf-8",
        )
        self.graph = RepositoryIndexer().index(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_indexes_java_declarations_and_evidence_edges(self) -> None:
        type_nodes = [
            node for node in self.graph.nodes.values()
            if node.type == NodeType.CLASS
        ]
        method_names = {
            node.name for node in self.graph.nodes.values()
            if node.type == NodeType.METHOD
        }
        self.assertIn("org.example.app.UseClient", {node.name for node in type_nodes})
        self.assertIn("org.example.app.UseClient.Nested", {node.name for node in type_nodes})
        self.assertTrue({"send", "helper"} <= method_names)
        self.assertTrue(any(
            edge.type == EdgeType.REFERENCES
            and edge.evidence.line is not None
            and edge.evidence.confidence > 0
            for edge in self.graph.edges
        ))

    def test_indexes_java_class_with_many_methods(self) -> None:
        content = (
            "package sample; public class ManyMethods {\n"
            + "".join(
                f'public String method{index}() {{ return "value"; }}\n'
                for index in range(60)
            )
            + "}"
        )
        record_path = self.root / "ManyMethods.java"
        record_path.write_text(content, encoding="utf-8")
        graph = RepositoryIndexer().index(self.root)
        method_names = {
            node.name for node in graph.nodes.values()
            if node.type == NodeType.METHOD
            and node.path == "ManyMethods.java"
        }
        self.assertEqual(len(method_names), 60)
        self.assertIn("method59", method_names)

    def test_indexes_maven_dependency_and_finds_potential_consumer(self) -> None:
        self.assertEqual(len(self.graph.dependencies), 1)
        dependency = self.graph.dependencies[0]
        self.assertEqual(dependency.version, "1.2.0")
        impacts = analyze_dependency_upgrade(
            self.graph,
            "org.example.client",
            "client-api",
            "2.0.0",
        )
        self.assertEqual(len(impacts), 1)
        self.assertEqual(
            [node.path for node in impacts[0].files],
            ["src/main/java/org/example/app/UseClient.java"],
        )
        self.assertEqual(len(impacts[0].modules), 1)
        self.assertEqual(impacts[0].confidence, "inferred")

    def test_compiles_context_under_budget(self) -> None:
        compiled = compile_context(
            self.graph,
            "Update the Client send method",
            token_budget=2_000,
            count_tokens=lambda text: len(text),
        )
        self.assertIn(
            "src/main/java/org/example/app/UseClient.java",
            compiled.files,
        )
        self.assertLessEqual(len(compiled.text), 2_000)

    def test_assigns_nested_maven_sources_to_nearest_module(self) -> None:
        child = self.root / "child"
        child_source = child / "src" / "main" / "java" / "org" / "example"
        child_source.mkdir(parents=True)
        (child / "pom.xml").write_text(
            "<project><artifactId>child</artifactId></project>",
            encoding="utf-8",
        )
        child_file = "child/src/main/java/org/example/ChildUse.java"
        (child_source / "ChildUse.java").write_text(
            "package org.example; import org.example.client.Client; "
            "class ChildUse { void run() {} }",
            encoding="utf-8",
        )
        graph = RepositoryIndexer().index(self.root)
        child_owners = {
            graph.nodes[edge.source].name
            for edge in graph.edges
            if edge.type == EdgeType.CONTAINS
            and edge.target == f"file:{child_file}"
            and edge.source in graph.nodes
            and graph.nodes[edge.source].type == NodeType.MODULE
        }
        self.assertEqual(child_owners, {"child"})


if __name__ == "__main__":
    unittest.main()
