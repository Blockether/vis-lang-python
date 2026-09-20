# vis-lang-python

The Python language pack for [Vis](https://github.com/Blockether/vis). It registers with
[vis-lang-interface](https://github.com/Blockether/vis-lang-interface) and serves Python:

- syntax verdicts from the interpreter's own compiler, for the write gate,
- `format_code` and `lint_code` through ruff,
- `run_tests` for pytest, unittest and a project's own runner,
- the managed project REPL behind `repl_start` / `repl_eval` / `repl_stop`.

## Status

Early: extraction from the Vis engine is in progress. Until the first release, pin by commit.
