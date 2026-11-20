# vis-lang-python

Python tools for [Vis](https://github.com/Blockether/vis): Ruff formatting and lint, pytest runs
and a managed project REPL. Tests and the REPL use your project's interpreter.

## Install

```bash
vis-agent extension install blockether/vis-lang-python --global --trust
```

Use Vis 0.2.53 or later for toolchain grants. Install pytest in each project you test. The
extension supplies Ruff when neither the project environment nor `PATH` has it.

## Use

| Tool | Purpose |
| --- | --- |
| `py.format_code` | Format files with Ruff. Set `is_written=True` to write the changes. |
| `py.lint_code` | Run `ruff check` and report rule codes. |
| `py.run_tests` | Run pytest. Set `coverage=True` to report line and branch coverage and gaps. |
| `py.repl_start` | Install project dependencies and start a REPL. |
| `py.repl_status` | Check if the project's REPL is active. |
| `py.repl_eval` | Evaluate Python in the active REPL. Variables stay available between calls. |
| `py.repl_stop` | Stop the project's REPL. |

```python
py.run_tests(["tests"], coverage=True)
```

Edit hooks check Python files and repair structural errors when safe. See the
[Python toolchain reference](docs/toolchain.md) for dependency installation, REPL behavior and
syntax checks. Results follow the [language contract](https://github.com/Blockether/vis-lang-interface).

## Configuration

To run tests and the REPL with Vis shared packages instead of the project's virtual environment,
set this in the project's `vis.yml`:

```yaml
python:
  environment: shared
```

## Development

```bash
vis-agent python -m pytest tests -q
```

The repair port and its regression corpus come from `clj-parinferish` under the MIT License.
See [NOTICE](NOTICE).
