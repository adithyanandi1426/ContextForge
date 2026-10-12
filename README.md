# ContextForge Explorer

ContextForge Explorer is a local-first Streamlit app that helps developers
prepare focused repository context for an AI coding task. It indexes source
files, shows the repository layout, and ranks files against a task description
before packaging the most relevant context for download.

## Why use it?

When asking an AI assistant to work in an unfamiliar codebase, it can be hard
to know which files, symbols, and tests to include. ContextForge provides:

- An interactive folder browser and full directory tree to quickly understand
  repository structure.
- A repository inventory with indexed files, detected symbols, imports, line
  counts, and content hashes.
- Task-based file ranking with explanations for why each candidate matched.
- A context budget so the generated package stays a manageable size.
- A downloadable Markdown context package that can be shared with a coding
  assistant.
- A full-repository digest powered by Gitingest, including its own directory
  tree and file contents.
- An external GitDiagram link for interactive diagrams of GitHub repositories.
- An interactive dependency and impact graph that traces a selected
  file/class/method to likely dependencies and impacted callers.
- A tokenizer-based input-token comparison between the full repository and
  task-ranked context.

The app does not call an LLM or execute code from the indexed repository.

## Requirements

- Python 3.10 or newer
- Git installed and available on `PATH` to clone GitHub repositories, or an
  internet connection so public GitHub repositories can be downloaded as ZIP
  archives when Git is unavailable
- Dependencies from `requirements.txt`

## Run the app

Open a terminal in the project directory and install the dependencies:

```powershell
python -m pip install -r requirements.txt
```

Start the app:

```powershell
python -m streamlit run app.py
```

Streamlit prints a local URL (usually `http://localhost:8501`) to open in your
browser.

## How to use it

1. Enter a GitHub repository URL or a path to a local repository in the
   **Repository** sidebar field.
2. Set the approximate context budget. The estimate uses four characters per
   token and is not tied to a specific model tokenizer.
3. Select **Index repository**.
4. Use the **Directory structure** tab's **Browse folder** selector to inspect
   folders one level at a time. For GitHub repositories, select **Open interactive
   GitDiagram** to view a diagram in the external GitDiagram service; use this
   with public repositories. The **Repository inventory** tab lists supported
   files and their detected metadata.
5. Open **Context builder**, describe the change you want to make, and select
   **Build context package**.
6. Review the selected files and match explanations, then download the
   generated Markdown package.
7. Share that package with your coding assistant along with the task.

To inspect cross-file impact, open **Impact graph**, enter a file, class, or
method name (or a question containing one), select the matching module, choose
a graph depth, and select **Trace dependencies and impact**. The interactive
graph supports hover, zoom, and pan. Hover symbol nodes for declaration and
reference line numbers; the tables list detected relationships and impacted
files with their class/method definitions.

The impact graph supports best-effort declaration and reference detection for
the indexed language set: Java, Kotlin, Python, JavaScript/TypeScript, Go, C#,
C/C++, Rust, and Ruby. The graph is a static name-reference analysis, not
compiler-resolved call hierarchy. Dynamic dispatch, reflection, generated
code, overloaded or duplicate symbol names, and language-specific import
resolution may be missed or ambiguous. Use the graph to navigate review, then
verify findings against the source and tests.

To create a full Gitingest digest, use **Create Gitingest digest** in the
sidebar. This works with local paths and GitHub repository URLs; GitHub
subdirectory URLs require Git to be installed. The result includes a summary
and file contents, and can be downloaded as `gitingest-digest.txt`. It is
separate from the task-ranked context package: the digest includes all files
Gitingest accepts, while the context builder ranks supported source files
against your task and observes its context budget.

To compare input-token usage, create a Gitingest digest, then build a context
package for the same repository and task. The comparison uses the same task
and coding instructions for both sides: the baseline adds the full Gitingest
tree and file contents, while the ContextForge package adds only selected
task-ranked files. Counts use the `cl100k_base` tokenizer, so they are
reproducible but may differ from another model's tokenizer. This measures
prompt input size only; it does not establish answer quality or output-token
savings.

For private GitHub repositories, enter a personal access token in the optional
password field, or set `GITHUB_TOKEN` in the environment before starting the
app. The token is passed only to Gitingest and is not included in the generated
digest. Enable the submodules option to include repository submodules. By
default, Gitingest respects `.gitignore`; enable the Git-ignored files option
only when those files should be included.

## What gets indexed

The app analyzes supported source and project file types, including Java,
Kotlin, Python, JavaScript/TypeScript, Go, C#, C/C++, Rust, Ruby, XML, JSON,
YAML, Gradle, Markdown, and SQL. Files larger than 350 KB are ignored.

The directory tree shows repository files and folders, including files that
are not analyzed as source. Generated or dependency directories such as
`.git`, `node_modules`, `target`, `build`, and virtual environments are omitted
from both the tree and the source-file scan.

GitHub repositories are cloned shallowly when Git is available. If Git cannot
be found, ContextForge downloads public repository ZIP archives instead.
Gitingest digests for root-level public repositories use the same archive
fallback. Private repositories require Git and suitable Git credentials.
