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

The REPL stays alive when the Vis sandbox restarts, for example after `/reload`. The next call
reaches the same interpreter, with its globals. Only `py.repl_stop` and the end of the session stop it.

## Keep files parseable

Edit hooks repair structural mistakes and validate syntax locally in Python.
They use `ast.parse` and compile the syntax tree without executing it.
They do not start a project interpreter, write bytecode files or change REPL globals.
Project lint and tests still use your project's tools and interpreter.

- Before a patch writes, the guard tries to repair invalid source within the changed lines.
  The host writes the validated result once and reports the corrections.
  If repair fails, a patch that breaks a parseable file writes nothing.
- Vis repairs a Python block itself before the block executes. This extension does not change blocks.
  Before each block, the guard records the state of the Python files for the check after the block.
- After a Python block, the guard can repair changed `.py` and `.pyi` files.
  These repairs happen after the original writes. The block is not transactional.
  Repairs include notes and diffs in `session["python_syntax_repairs"]`.
  Unresolved errors remain in `session["python_syntax_errors"]` until the files parse again.
- Before `repl_eval` runs code, the project's interpreter compiles all of it.
  If the code does not parse, none of it runs, and the guard's repair is tried on the code.
  When the interpreter accepts the repaired code, it runs, and the result lists each correction in `repairs`.
  If no safe repair exists, the error gives the line, the column and the parser's message.

The shared [syntax guard](https://github.com/Blockether/vis-lang-interface#keep-source-files-parseable)
selects matching files and prevents a repair from overwriting a detected concurrent change.
Repairs require a Vis host with support for repair decisions.
If the parser is unavailable, the guard allows the operation and logs the failure.
It never accepts a repair without successful validation.

## Requirements

`pytest` in the project you test. Everything else is installed with the extension.

## Development

```bash
vis-agent python -m pytest tests -q
```

Results follow the contract in
[vis-lang-interface](https://github.com/Blockether/vis-lang-interface).
The repair port and its regression corpus come from `clj-parinferish` under the MIT License.
See [NOTICE](NOTICE) for the required license notice.
