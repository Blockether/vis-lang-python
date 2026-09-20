# vis-lang-python

Python tools for [Vis](https://github.com/Blockether/vis): ruff formatting and lint, pytest runs
and a REPL that keeps its globals between calls.

Vis knows nothing about Python. This extension does, and it runs the tools your project already
uses — its ruff, its pytest, its interpreter — so what you see here matches what your CI sees.

## Install

```bash
vis-agent extension install 'blockether/vis-lang-python' --global --trust
```

## Tools

```python
py.format_code(["src"], is_written=True)     # ruff format
py.lint_code(["src"])                        # ruff check, with rule codes
py.run_tests(["tests"], keyword="repl")      # pytest, counted from its JUnit report
py.repl_start(cwd="~/app")                   # the project's own interpreter
py.repl_eval("model = load()", cwd="~/app")  # globals live between calls
py.repl_stop(cwd="~/app")
```

Which interpreter runs your tests and REPL is decided per project, first match winning: uv
(`uv.lock` or `[tool.uv]`), Poetry (`poetry.lock`), a local `.venv`, then `python3`. Ruff is taken
from the project's virtualenv, then `PATH`, then the copy installed with this extension.

## Requirements

`pytest` in the project you test. Everything else is installed with the extension.

## Development

```bash
vis-agent python -m pytest tests -q
```

Results follow the contract in
[vis-lang-interface](https://github.com/Blockether/vis-lang-interface).
