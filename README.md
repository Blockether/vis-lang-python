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
py.run_tests(["tests"], max_failures=1)      # stop at the first failure
py.run_tests(["tests"], coverage=True)       # + coverage.py lines, branches and gaps
py.repl_start(cwd="~/app")                   # install the dependencies, then the project's interpreter
py.repl_eval("model = load()", cwd="~/app")  # globals live between calls
py.repl_stop(cwd="~/app")
```

Which interpreter runs your tests and REPL is decided per project, first match winning: uv
(`uv.lock` or `[tool.uv]`), Poetry (`poetry.lock`), a local `.venv`, then `python3`. Ruff is taken
from the project's virtualenv, then `PATH`, then the copy installed with this extension.

If the project's `vis.yml` sets `python: {environment: shared}`, tests and the REPL run with
`vis-agent python --shared` instead. They then import Vis shared packages and never use `.venv`.
When a broken `.venv` stops pytest or the REPL, the error names both fixes.

`py.repl_start` installs the project's dependencies before the interpreter starts:

| The project has | The start runs | The REPL runs in |
| --- | --- | --- |
| `pyproject.toml` with `[project]`, a `dev` group in `[dependency-groups]`, or a `uv.lock` | `vis-agent python uv run` | the project's `.venv`, which uv makes |
| the same without `uv.lock` and `[tool.uv]`, and a `.venv` or `venv` that you made | `vis-agent python uv pip install -r pyproject.toml` (and `--group dev`) | that environment; uv writes no lock, makes no second environment and does not install the project itself |
| only `requirements.txt` | `vis-agent python uv pip install -r requirements.txt` | `.venv` or `venv`; `.venv` is made when both are absent |
| `python: {environment: shared}` in `vis.yml` | `vis-agent python --shared uv sync` for a uv project, else `uv pip install -r pyproject.toml` or `-r requirements.txt` | Vis shared packages, no `.venv` |
| `poetry.lock` without a uv marker, or no dependency file | nothing | as the list above says |

- `pyproject.toml` wins over `requirements.txt`. A `pyproject.toml` with only tool configuration does
  not count. A `[tool.uv]` table alone makes a uv project only when there is no `requirements.txt`.
- The installer is the uv that Vis ships. Vis says where it is, so `vis-agent` does not need to be on
  `PATH`: Vis can be a native binary in a container. A `uv` on `PATH` is used only when the process
  has no Vis, or on a Vis host without `vis.python_runtime` and without `vis-agent` on `PATH`.
- When uv makes the `.venv` of a `pyproject.toml` project, it also writes a `uv.lock`. Packages that
  you installed by hand stay.
- A REPL in a virtualenv has that environment's `bin` directory first on `PATH`, and `VIRTUAL_ENV`
  set.
- Starts of one project run one at a time. A second start waits, then finds the REPL of the first.
- A successful installation leaves a stamp in `~/.cache/vis-lang-python` (`VIS_LANG_PYTHON_CACHE`).
  While the dependency files and the installed distributions stay the same, the next start runs no
  installer. A changed file, a deleted environment or a package that you install or remove by hand
  makes the next start install again.
- An installation can take 900 seconds. The result's `detail` says what the start did.
- When the installation fails, the `detail` has the installer's command and last output. The REPL
  then runs on the environment that exists. With no environment, the start fails with that report,
  and the `.venv` and the `uv.lock` that the failed installer made are removed. What existed before
  the start stays.
- A `.venv` that is a virtualenv without an interpreter cannot run. uv makes it again, and the
  `detail` says so. A `.venv` directory that is no virtualenv is never cleared.
- A live REPL gets no installation. When the dependency files changed after its start, the result
  says to stop and start it.
- `py.repl_start(upgrade=True)` installs the newest versions that the files allow, at that one
  start: `uv run --upgrade`, or `uv pip install --upgrade`. A pinned version stays. A uv project
  gets its `uv.lock` upgraded; with shared packages that is `uv lock --upgrade`, then `uv sync`.
  The `detail` then names each changed package with its versions, or says that no newer version
  was installed. A start without `upgrade` never asks the index when nothing changed. With a live
  REPL, `upgrade=True` is refused: stop the REPL first.
- A project environment without `pip` gets the `pip` of its own Python (`python -m ensurepip`), so
  `python -m pip install` works in the REPL. This needs no index. An environment that has `pip`
  keeps it. When `pip` cannot be added, the start continues and the `detail` says so. An exact
  `uv sync` that you run removes this `pip`; the next start adds it again.
- In a shared REPL, Vis makes `sys.executable` an environment of its shared packages, so
  `python -m pip install` there installs into them. A Vis older than this installs into its own
  Python runtime: run `vis-agent update`. To change or remove a shared package, use
  `vis-agent python --shared uv pip install` or `uninstall`.

The REPL stays alive when the Vis sandbox restarts, for example after `/reload`. The next call
reaches the same interpreter, with its globals. Only `py.repl_stop` and the end of the session stop it.

When Vis confines a session, its jail reads only the system directories, the workspace and granted
paths. `vis-agent`, `uv`, Poetry or ruff under your home directory, for example in `~/.local/bin`,
are outside them. The extension declares each of these programs on `PATH`, the program that starts
Vis and the files that they link to as read-only grants. It declares the uv, Poetry and pip caches as read-write grants, so a
confined run reuses your downloads. Vis checks the grants and gives them only to the extension's own
processes, not to the agent.
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

Vis 0.2.53 or later, for toolchain grants. `pytest` in the project you test. Everything else is
installed with the extension.

## Development

```bash
vis-agent python -m pytest tests -q
```

Results follow the contract in
[vis-lang-interface](https://github.com/Blockether/vis-lang-interface).
The repair port and its regression corpus come from `clj-parinferish` under the MIT License.
See [NOTICE](NOTICE) for the required license notice.
