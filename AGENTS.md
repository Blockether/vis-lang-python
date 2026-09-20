# vis-lang-python

Python only: ruff, pytest and a managed interpreter. No JVM, no parser of our own, no tree-sitter.

- `src/vis_lang_python/` is ordinary Python; `extension.py` is the only file that mentions Vis.
- Results are the contract types from `vis-lang-interface`. Do not invent a second result shape;
  extend the contract there and bump both versions together.
- Run the project's own tools: its ruff, its pytest, its interpreter. Falling back to the bundled
  ruff is fine; silently using this extension's interpreter for a project's tests is not.
- Every exported method owns an explicit Activity presentation with a capitalized English label.
  Cover success, failure and empty states in tests.
- `repl.py` owns one child process per project directory. Closing its stdin ends it; never leave
  an interpreter behind, and never replace a live REPL, because its globals are the user's work.
- Formatting and lint are ruff. Tests: `vis-agent python -m pytest tests -q`.
