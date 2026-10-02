# vis-lang-python

Python only: ruff, pytest and a managed interpreter. No JVM, no parser of our own, no tree-sitter.

- `src/vis_lang_python/` is ordinary Python. Only `extension.py` mentions Vis.
- Return the contract types from `vis-lang-interface`. Do not make a second result shape: extend the contract there and bump both versions together.
- Run the project's own tools: its ruff, its pytest and its interpreter. A fallback to the bundled ruff is fine. Never run a project's tests silently with this extension's interpreter.
- Give every exported method an explicit Activity presentation with a capitalized English label. Test the success, failure and empty states.
- `repl.py` owns one child process for each project directory; closing its stdin ends it. Never leave an interpreter behind. Never replace a live REPL, because its globals are the user's work.
- Format and lint with ruff. Run tests with `vis-agent python -m pytest tests -q`.
