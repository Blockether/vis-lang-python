"""Vis entrypoint. The tools themselves live in vis_lang_python."""

import blockether.vis.extension as vis
from vis_lang_interface import presentation, prompt

from vis_lang_python.tools import PythonTools


def _bind(name, label, build, *, tag="observation", show_start=True):
    """Attach one Activity presentation to a method of PythonTools."""
    setattr(
        PythonTools,
        name,
        vis.method(
            tag=tag,
            activity=presentation.activity(label, build, show_start=show_start),
        )(getattr(PythonTools, name)),
    )


_bind(
    "format_code",
    "Format Python code",
    lambda result: presentation.format_presentation("Format Python code", result),
    tag="mutation",
    show_start=False,
)
_bind(
    "lint_code",
    "Lint Python code",
    lambda result: presentation.lint_presentation("Lint Python code", result),
)
_bind(
    "run_tests",
    "Run Python tests",
    lambda result: presentation.test_presentation("Run Python tests", result),
)
_bind(
    "repl_start",
    "Start Python REPL",
    lambda result: presentation.session_presentation("Start Python REPL", result),
    tag="mutation",
)
_bind(
    "repl_status",
    "Check Python REPL",
    lambda result: presentation.session_presentation("Check Python REPL", result),
    show_start=False,
)
_bind(
    "repl_stop",
    "Stop Python REPL",
    lambda result: presentation.session_presentation("Stop Python REPL", result),
    tag="mutation",
    show_start=False,
)
_bind(
    "repl_eval",
    "Evaluate in Python REPL",
    lambda result: presentation.repl_presentation("Evaluate in Python REPL", result),
    tag="mutation",
)

PROMPT = prompt.routing(
    "Python",
    "py",
    (
        "format_code",
        "lint_code",
        "run_tests",
        "repl_start",
        "repl_status",
        "repl_eval",
        "repl_stop",
    ),
    notes=(
        "`py.repl_start` uses the project's own interpreter — uv, Poetry, a local virtualenv or"
        " python3, in that order — so the REPL sees the project's packages, which the sandbox"
        " block does not; `py.repl_eval` needs that interpreter already started.",
        "`py.run_tests` runs pytest under the same interpreter and needs no REPL;"
        " `py.format_code` and `py.lint_code` are ruff.",
    ),
)

vis.register_extension(
    vis.Extension(
        name="vis-lang-python",
        description="Python tools: ruff formatting and lint, pytest runs and a managed project REPL.",
        version="1.3.2",
        alias="py",
        symbols=[vis.Symbol(PythonTools(workspace_root=vis.workspace_root), name="py")],
        prompt=PROMPT,
    )
)
