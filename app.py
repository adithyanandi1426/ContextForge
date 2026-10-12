from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from gitingest import ingest
from impact_graph import (
    build_impact_graph,
    extract_definitions,
    find_impact_targets,
)
import streamlit as st
import tiktoken


st.set_page_config(
    page_title="ContextForge Explorer",
    page_icon="🧠",
    layout="wide",
)

SOURCE_EXTENSIONS = {
    ".java", ".kt", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".go", ".cs", ".cpp", ".h", ".hpp", ".rs", ".rb",
    ".xml", ".yaml", ".yml", ".json", ".properties",
    ".gradle", ".md", ".sql",
}

SKIP_DIRS = {
    ".git", ".idea", ".vscode", "node_modules", "vendor",
    "target", "build", "dist", "out", ".gradle",
    "__pycache__", ".venv", "venv", "coverage",
}

MAX_FILE_BYTES = 350_000

# Lightweight symbol patterns for a demo, not a full parser.
SYMBOL_PATTERNS = [
    re.compile(
        r"^\s*(?:public|protected|private|static|final|abstract|"
        r"synchronized|default|\s)*"
        r"(?:class|interface|enum|record)\s+(\w+)",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:public|protected|private|static|final|abstract|"
        r"synchronized|default|\s)+[\w<>, ?.\[\]]+\s+"
        r"(\w+)\s*\([^;]*\)\s*(?:throws [^{]+)?\{",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:async\s+)?def\s+(\w+)|"
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)",
        re.MULTILINE,
    ),
]


def download_github_archive(owner: str, repo: str, destination: Path) -> Path:
    request = Request(
        f"https://api.github.com/repos/{owner}/{repo}/zipball",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ContextForge",
        },
    )
    archive_path = destination / "repository.zip"
    with (
        urlopen(request, timeout=30) as response,
        archive_path.open("wb") as archive_file,
    ):
        shutil.copyfileobj(response, archive_file)

    with zipfile.ZipFile(archive_path) as archive:
        root_names = set()
        for member in archive.infolist():
            name = member.filename
            member_path = PurePosixPath(name)
            if (
                not name
                or not member_path.parts
                or "\\" in name
                or member_path.is_absolute()
                or ".." in member_path.parts
                or ":" in member_path.parts[0]
            ):
                raise ValueError("GitHub returned an unsafe archive path.")

            root_names.add(member_path.parts[0])

            mode = member.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError("GitHub returned an archive containing a symlink.")

        if len(root_names) != 1:
            raise ValueError("GitHub returned an unexpected repository archive.")

        archive.extractall(destination)

    root = destination / root_names.pop()
    if not root.is_dir():
        raise ValueError("GitHub repository archive has no root directory.")
    return root


def clone_or_open(source: str) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    source = source.strip()

    if source.startswith(("https://", "http://")):
        # Restrict the demo to GitHub URLs. Never pass shell commands
        # through a shell, and never execute repository code.
        from urllib.parse import urlparse

        parsed = urlparse(source)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("For remote repositories, use a GitHub URL.")

        if parsed.query or parsed.fragment or ".." in parsed.path.split("/"):
            raise ValueError("Unexpected URL format.")

        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2:
            raise ValueError("Use a URL like https://github.com/org/repo")

        repo_name = parts[1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        repo_url = f"https://github.com/{parts[0]}/{repo_name}"
        repo_url += ".git"

        temp = tempfile.TemporaryDirectory(prefix="contextforge-")
        try:
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", "--", repo_url, temp.name],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                root = Path(temp.name)
            except FileNotFoundError as exc:
                try:
                    root = download_github_archive(
                        parts[0], repo_name, Path(temp.name)
                    )
                except Exception as archive_exc:
                    raise RuntimeError(
                        "Git is not installed or not available in PATH, and "
                        f"GitHub archive download failed: {archive_exc}"
                    ) from exc
        except Exception:
            temp.cleanup()
            raise
        return root, temp

    path = Path(source).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("Enter a valid local repository directory.")
    return path, None


def discover_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    return sorted(files)


def build_directory_index(root: Path) -> dict[str, list[tuple[str, bool]]]:
    folders: dict[str, list[tuple[str, bool]]] = {}

    def index_children(directory: Path, relative: Path) -> None:
        entries = []
        with os.scandir(directory) as children:
            for child in children:
                is_directory = child.is_dir(follow_symlinks=False)
                if is_directory and child.name in SKIP_DIRS:
                    continue
                child_relative = relative / child.name
                entries.append((child.name, is_directory))
                if is_directory:
                    index_children(Path(child.path), child_relative)

        entries.sort(key=lambda entry: (not entry[1], entry[0].casefold()))
        folder_key = relative.as_posix() if relative.parts else "."
        folders[folder_key] = entries

    index_children(root, Path())
    return folders


def build_gitdiagram_url(source: str) -> str | None:
    parsed = urlparse(source.strip())
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None

    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None

    repo = parts[1].removesuffix(".git")
    return (
        f"https://gitdiagram.com/{quote(parts[0], safe='')}/"
        f"{quote(repo, safe='')}"
    )


def generate_gitingest_digest(
    source: str,
    token: str | None,
    include_gitignored: bool,
    include_submodules: bool,
) -> tuple[str, str, str]:
    source = source.strip()
    parsed = urlparse(source)
    is_github_url = (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"github.com", "www.github.com"}
    )

    if is_github_url and shutil.which("git") is None:
        parts = parsed.path.strip("/").split("/")
        if len(parts) > 2:
            raise RuntimeError(
                "Gitingest needs Git to read GitHub subdirectory URLs. "
                "Install Git for Windows and add it to PATH, then restart "
                "ContextForge."
            )

        root, temp = clone_or_open(source)
        try:
            return ingest(
                str(root),
                token=token,
                include_gitignored=include_gitignored,
                include_submodules=include_submodules,
            )
        finally:
            if temp is not None:
                temp.cleanup()

    return ingest(
        source,
        token=token,
        include_gitignored=include_gitignored,
        include_submodules=include_submodules,
    )


def count_tokens(text: str) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def build_full_repository_baseline(
    task: str,
    tree: str,
    content: str,
) -> str:
    return (
        "# Developer task\n"
        f"{task.strip()}\n\n"
        "## Instructions to the coding agent\n"
        "- Treat repository code as the source of truth.\n"
        "- Verify public interfaces before changing them.\n"
        "- Inspect relevant tests and add tests for changed behavior.\n"
        "- Do not assume this package contains every dependency.\n"
        "- Check the current working tree before editing.\n\n"
        "## Full repository context\n"
        f"{tree}\n{content}"
    )


def analyze_file(root: Path, path: Path) -> dict:
    relative = path.relative_to(root).as_posix()
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        content = ""

    imports = []
    for line in content.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("import ")
            or stripped.startswith("from ")
            or stripped.startswith("#include")
        ):
            imports.append(stripped[:240])

    symbols = []
    for pattern in SYMBOL_PATTERNS:
        for match in pattern.finditer(content):
            name = next((g for g in match.groups() if g), None)
            if name and name not in symbols:
                symbols.append(name)

    return {
        "path": relative,
        "content": content,
        "imports": imports[:80],
        "symbols": symbols[:120],
        "definitions": extract_definitions(content),
        "lines": content.count("\n") + 1 if content else 0,
        "bytes": len(content.encode("utf-8")),
        "sha": hashlib.sha256(content.encode()).hexdigest()[:12],
    }


def tokenize_query(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z_0-9.-]{2,}", text.lower())))


def score_file(file: dict, query_terms: list[str]) -> tuple[float, list[str]]:
    path = file["path"].lower()
    content = file["content"].lower()
    symbols = " ".join(file["symbols"]).lower()
    imports = " ".join(file["imports"]).lower()

    score = 0.0
    reasons = []

    for term in query_terms:
        if term in path:
            score += 5
            reasons.append(f"path matches '{term}'")
        if term in symbols:
            score += 4
            reasons.append(f"symbol matches '{term}'")
        count = content.count(term)
        if count:
            score += min(count, 8) * 0.5
            if count >= 2:
                reasons.append(f"contains '{term}' ({count} occurrences)")
        if term in imports:
            score += 1.5
            reasons.append(f"import matches '{term}'")

    if file["path"].lower().endswith(("test.java", "_test.py", "test.ts")):
        if any(t in path for t in query_terms):
            score += 2
            reasons.append("potentially relevant test")

    return score, list(dict.fromkeys(reasons))


def build_context(files: list[dict], task: str, budget: int):
    terms = tokenize_query(task)
    ranked = []

    for file in files:
        score, reasons = score_file(file, terms)
        if score > 0:
            ranked.append((score, file, reasons))

    ranked.sort(key=lambda item: (-item[0], item[1]["path"]))

    # A practical demo approximation: roughly four characters per token.
    # Real token counts depend on the model tokenizer and language.
    char_budget = budget * 4
    used_chars = 0
    selected = []

    for score, file, reasons in ranked:
        header = (
            f"\n## File: {file['path']}\n"
            f"Relevance score: {score:.1f}\n"
            f"Selection rationale: {'; '.join(reasons[:8])}\n"
        )
        available = char_budget - used_chars - len(header)
        if available <= 0:
            break

        content = file["content"]
        if len(content) > available:
            # Don't silently present a truncated file as complete.
            # Keep a bounded snippet and mark it clearly.
            content = content[:available]
            content += "\n\n[TRUNCATED BY CONTEXT BUDGET]\n"

        block = header + "\n```text\n" + content + "\n```\n"
        if used_chars + len(block) > char_budget:
            continue

        selected.append({
            "path": file["path"],
            "score": score,
            "reasons": reasons,
            "text": block,
        })
        used_chars += len(block)

    package = (
        "# ContextForge Task Context\n\n"
        "## Developer task\n"
        f"{task.strip()}\n\n"
        "## Instructions to the coding agent\n"
        "- Treat repository code as the source of truth.\n"
        "- Verify public interfaces before changing them.\n"
        "- Inspect relevant tests and add tests for changed behavior.\n"
        "- Do not assume this package contains every dependency.\n"
        "- Check the current working tree before editing.\n\n"
        "## Selected repository context\n"
        + "".join(item["text"] for item in selected)
    )

    return ranked, selected, package, used_chars


st.title("🧠 ContextForge Explorer")
st.caption(
    "Compile task-specific code context for AI developers. "
    "Local-first prototype; no LLM API key required."
)

with st.sidebar:
    st.header("Repository")
    source = st.text_input(
        "GitHub URL or local path",
        value="https://github.com/cloudslang/cs-actions",
    )
    budget = st.slider(
        "Context budget (approx. tokens)",
        min_value=1000,
        max_value=30000,
        value=8000,
        step=500,
    )
    st.caption("Token estimates use characters / 4, not a model tokenizer.")
    github_token = st.text_input(
        "GitHub token (optional)",
        type="password",
        help="Used only by Gitingest for private GitHub repositories. "
        "If blank, GITHUB_TOKEN is used when available.",
    )
    include_gitignored = st.checkbox(
        "Include Git-ignored files in Gitingest digest",
        value=False,
    )
    include_submodules = st.checkbox(
        "Include repository submodules in Gitingest digest",
        value=False,
    )
    create_digest = st.button(
        "Create Gitingest digest",
        use_container_width=True,
    )

    analyze = st.button("Index repository", type="primary", use_container_width=True)

if analyze:
    temp = None
    try:
        with st.spinner("Loading repository and building lightweight index..."):
            root, temp = clone_or_open(source)
            directory_index = build_directory_index(root)
            paths = discover_files(root)
            files = [analyze_file(root, p) for p in paths]

            if not files:
                raise ValueError("No supported source files were found.")

            st.session_state["files"] = files
            st.session_state["directory_index"] = directory_index
            st.session_state.pop("directory_browser_folder", None)
            st.session_state.pop("impact_graph", None)
            st.session_state["root_label"] = source
            st.session_state["index_summary"] = {
                "files": len(files),
                "symbols": sum(len(f["symbols"]) for f in files),
                "lines": sum(f["lines"] for f in files),
                "extensions": Counter(
                    Path(f["path"]).suffix or "(no extension)" for f in files
                ),
            }
            st.session_state["indexed"] = True

        st.success("Repository indexed.")
    except Exception as exc:
        st.error(f"Indexing failed: {exc}")
    finally:
        if temp is not None:
            temp.cleanup()

if create_digest:
    st.session_state.pop("gitingest_digest", None)
    st.session_state.pop("token_comparison", None)
    try:
        with st.spinner("Generating repository digest with Gitingest..."):
            digest_summary, digest_tree, digest_content = generate_gitingest_digest(
                source.strip(),
                github_token.strip() or None,
                include_gitignored,
                include_submodules,
            )
            st.session_state["gitingest_digest"] = {
                "source": source.strip(),
                "summary": digest_summary,
                "tree": digest_tree,
                "content": digest_content,
            }
    except Exception as exc:
        details = str(exc) or "No additional error details were provided."
        st.error(f"Gitingest digest failed ({type(exc).__name__}): {details}")

if "gitingest_digest" in st.session_state:
    digest = st.session_state["gitingest_digest"]
    with st.expander(
        f"Gitingest digest: {digest['source']}",
        expanded=True,
    ):
        st.text(digest["summary"])
        st.caption(
            f"Full digest generated ({len(digest['content']):,} characters). "
            "Download it to inspect the contents; it is retained for the "
            "token comparison."
        )
        st.download_button(
            "Download Gitingest digest",
            data=(
                f"{digest['summary']}\n\n"
                f"{digest['tree']}\n{digest['content']}"
            ),
            file_name="gitingest-digest.txt",
            mime="text/plain",
        )

if st.session_state.get("indexed"):
    files = st.session_state["files"]
    summary = st.session_state["index_summary"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Files indexed", summary["files"])
    c2.metric("Symbols detected", summary["symbols"])
    c3.metric("Source lines", f"{summary['lines']:,}")
    c4.metric("Extensions", len(summary["extensions"]))

    directory_tab, inventory_tab, context_tab, impact_tab = st.tabs(
        [
            "Directory structure",
            "Repository inventory",
            "Context builder",
            "Impact graph",
        ]
    )

    with directory_tab:
        st.caption(
            "Browse folders interactively. For GitHub repositories, open "
            "GitDiagram for an interactive architecture diagram."
        )
        diagram_url = build_gitdiagram_url(st.session_state["root_label"])
        if diagram_url:
            st.link_button(
                "Open interactive GitDiagram",
                diagram_url,
                help="Opens GitDiagram, an external service. Use this for "
                "public repositories.",
            )
        else:
            st.info("GitDiagram is available for GitHub repository URLs.")

        folder_index = st.session_state["directory_index"]
        root_label = st.session_state["root_label"].rstrip("/\\")
        folders = sorted(
            (folder for folder in folder_index if folder != "."),
            key=lambda folder: (folder.count("/"), folder.casefold()),
        )
        selected_folder = st.selectbox(
            "Browse folder",
            options=["."] + folders,
            format_func=lambda folder: (
                root_label
                if folder == "."
                else f"{root_label} / {folder}"
            ),
            key="directory_browser_folder",
        )
        contents = folder_index[selected_folder]
        st.caption(
            f"{len(contents)} item(s) in {root_label}"
            + ("" if selected_folder == "." else f"/{selected_folder}")
        )
        if contents:
            for name, is_directory in contents:
                st.markdown(f"- {'📁' if is_directory else '📄'} `{name}`")
        else:
            st.info("This folder is empty.")

    with inventory_tab:
        st.write(dict(summary["extensions"]))
        st.dataframe(
            [{
                "File": f["path"],
                "Symbols": ", ".join(f["symbols"][:12]),
                "Imports": len(f["imports"]),
                "Lines": f["lines"],
                "SHA": f["sha"],
            } for f in files],
            use_container_width=True,
            hide_index=True,
        )

    with impact_tab:
        st.subheader("Trace module impact")
        st.caption(
            "Enter a file, class, or method name to trace likely dependencies "
            "and impacted callers through the repository."
        )
        impact_query = st.text_input(
            "Question or module, class, or method",
            placeholder="For example: What is impacted by HttpClientService?",
        )
        impact_targets = find_impact_targets(files, impact_query)
        if impact_query and not impact_targets:
            st.info(
                "No matching file, class, or method was found in the indexed "
                "repository."
            )
        elif impact_targets:
            target_path = st.selectbox(
                "Select the target module",
                options=impact_targets,
                key="impact_target_path",
            )
            impact_depth = st.slider(
                "Graph depth (file hops)",
                min_value=1,
                max_value=4,
                value=2,
                key="impact_graph_depth",
            )
            if st.button("Trace dependencies and impact"):
                with st.spinner("Building the impact graph..."):
                    impact_figure, impact_summary = build_impact_graph(
                        files,
                        target_path,
                        impact_depth,
                    )
                    st.session_state["impact_graph"] = {
                        "query": impact_query,
                        "target": target_path,
                        "depth": impact_depth,
                        "figure": impact_figure,
                        "summary": impact_summary,
                    }

            graph_result = st.session_state.get("impact_graph")
            if graph_result and graph_result["query"] == impact_query:
                if (
                    graph_result["target"] != target_path
                    or graph_result["depth"] != impact_depth
                ):
                    st.info("Trace the newly selected target to refresh the graph.")
                else:
                    impact_summary = graph_result["summary"]
                    impact_metrics = st.columns(4)
                    impact_metrics[0].metric(
                        "Direct callers",
                        len(impact_summary["direct_impact"]),
                    )
                    impact_metrics[1].metric(
                        "Transitive callers",
                        len(impact_summary["transitive_impact"]),
                    )
                    impact_metrics[2].metric(
                        "Dependencies",
                        len(impact_summary["dependencies"]),
                    )
                    impact_metrics[3].metric(
                        "Graph files",
                        impact_summary["files_shown"],
                    )
                    st.plotly_chart(
                        graph_result["figure"],
                        use_container_width=True,
                        config={"scrollZoom": True, "displaylogo": False},
                    )
                    if impact_summary["truncated"]:
                        st.warning(
                            "The graph is limited to the closest 100 files "
                            "and 250 symbol relationships for readability. "
                            "Impact counts include all detected relationships."
                        )
                    if impact_summary["ambiguous_symbols_omitted"]:
                        st.caption(
                            f"{impact_summary['ambiguous_symbols_omitted']} "
                            "non-unique symbol name(s) were omitted from edges "
                            "to avoid guessing between definitions."
                        )
                    st.caption(impact_summary["evidence_note"])
                    if impact_summary["relationships"]:
                        st.subheader("Detected class and method references")
                        st.dataframe(
                            impact_summary["relationships"],
                            use_container_width=True,
                            hide_index=True,
                        )
                    if impact_summary["impacted_details"]:
                        st.subheader("Impacted files, classes, and methods")
                        st.dataframe(
                            impact_summary["impacted_details"],
                            use_container_width=True,
                            hide_index=True,
                        )
                    elif not impact_summary["dependencies"]:
                        st.info(
                            "No cross-file class or method references were "
                            "detected for this target."
                        )

    with context_tab:
        st.subheader("Compile context for a development task")
        task = st.text_area(
            "What are you trying to change?",
            value=(
                "Add retry handling to the HTTP client. "
                "Preserve public action contracts and identify relevant tests."
            ),
            height=100,
        )

        if st.button("Build context package", type="primary"):
            ranked, selected, package, used_chars = build_context(
                files, task, budget
            )
            st.session_state["package"] = package
            st.session_state["selected"] = selected
            st.session_state["ranked"] = ranked
            st.session_state["used_chars"] = used_chars
            st.session_state["package_task"] = task
            st.session_state["package_budget"] = budget
            st.session_state.pop("token_comparison", None)

        if "package" in st.session_state:
            selected = st.session_state["selected"]
            package = st.session_state["package"]
            used_chars = st.session_state["used_chars"]

            digest = st.session_state.get("gitingest_digest")
            if (
                digest is not None
                and digest["source"] == source.strip()
                and st.session_state.get("package_task") == task
                and st.session_state.get("package_budget") == budget
            ):
                if "token_comparison" not in st.session_state:
                    full_baseline = build_full_repository_baseline(
                        task,
                        digest["tree"],
                        digest["content"],
                    )
                    baseline_tokens = count_tokens(full_baseline)
                    context_tokens = count_tokens(package)
                    token_difference = baseline_tokens - context_tokens
                    percent_saved = (
                        token_difference / baseline_tokens * 100
                        if baseline_tokens
                        else 0.0
                    )
                    st.session_state["token_comparison"] = {
                        "baseline_tokens": baseline_tokens,
                        "context_tokens": context_tokens,
                        "token_difference": token_difference,
                        "percent_saved": percent_saved,
                    }

                comparison = st.session_state["token_comparison"]

                st.subheader("Token usage comparison")
                baseline_metric, context_metric, savings_metric = st.columns(3)
                baseline_metric.metric(
                    "Full repository baseline",
                    f"{comparison['baseline_tokens']:,}",
                )
                context_metric.metric(
                    "Task-ranked context",
                    f"{comparison['context_tokens']:,}",
                )
                token_change = comparison["token_difference"]
                savings_metric.metric(
                    (
                        "Input tokens saved"
                        if token_change >= 0
                        else "Input token increase"
                    ),
                    f"{abs(comparison['percent_saved']):.1f}%",
                    delta=f"{token_change:+,} tokens",
                )
                st.caption(
                    "Both counts use the same task and coding instructions. "
                    "The baseline includes the full Gitingest tree and file "
                    "contents; ContextForge uses task-ranked files. Counts "
                    "use the cl100k_base tokenizer and are not a measure of "
                    "answer quality or output-token savings."
                )
            else:
                st.info(
                    "Create a Gitingest digest for this same repository, then "
                    "build the context package with the current task and "
                    "budget to compare input tokens."
                )

            a, b, c = st.columns(3)
            a.metric("Files selected", len(selected))
            b.metric("Estimated tokens", f"~{used_chars // 4:,}")
            c.metric(
                "Candidate files",
                len(st.session_state["ranked"]),
            )

            st.subheader("Why these files?")
            if selected:
                st.dataframe(
                    [{
                        "File": item["path"],
                        "Score": round(item["score"], 1),
                        "Reason": "; ".join(item["reasons"][:4]),
                    } for item in selected],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.warning(
                    "No files matched the task keywords. Try more specific "
                    "class names, method names, or subsystem terms."
                )

            st.subheader("Generated context")
            st.code(package, language="markdown")

            st.download_button(
                "Download context package",
                data=package,
                file_name="contextforge-context.md",
                mime="text/markdown",
                type="primary",
            )
else:
    st.info("Index a repository to begin.")