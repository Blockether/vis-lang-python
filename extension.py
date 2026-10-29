"""Vis entrypoint. The tools themselves live in vis_lang_python."""

import blockether.vis.extension as vis
from vis_lang_interface import presentation, prompt
from vis_lang_interface.syntax import SyntaxGuard

from vis_lang_python.caches import cache_paths
from vis_lang_python.repair import repair_source
from vis_lang_python.toolchain import grantable, toolchain_paths
from vis_lang_python.tools import SYNTAX_SUFFIXES, PythonTools, _check_syntax


def _bind(
    name,
    label,
    build,
    *,
    tag: vis.SymbolTag = "observation",
    show_start=True,
    describe=None,
):
    """Attach one Activity presentation to a method of PythonTools."""
    setattr(
        PythonTools,
        name,
        vis.method(
            tag=tag,
            activity=presentation.activity(
                label, build, show_start=show_start, describe=describe
            ),
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
    tag="verification",
)
_bind(
    "run_tests",
    "Run Python tests",
    lambda result: presentation.test_presentation("Run Python tests", result),
    tag="verification",
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
    describe=presentation.code_argument("python"),
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
        "`py.repl_start` uses the project's own interpreter (uv, Poetry, a local virtualenv or"
        " python3, in that order), so the REPL sees the project's packages; the sandbox block does"
        " not. When vis.yml sets `python.environment: shared`, it uses `vis-agent python --shared`"
        " and never the project's `.venv`. `py.repl_eval` needs that interpreter started first.",
        "`py.run_tests` runs pytest with the same interpreter and needs no REPL. `py.format_code`"
        " and `py.lint_code` run ruff.",
        "`py.run_tests(coverage=True)` runs pytest under the project's coverage.py. Its result's"
        " `coverage` lists each file's gaps: the lines and branches that never ran.",
    ),
)

GUARD = SyntaxGuard("python", SYNTAX_SUFFIXES, _check_syntax, repair=repair_source)

# Vis gives these paths only to this extension's own processes.
GRANTS = [
    *(
        vis.Grant(path, reason="Run the Python toolchain for REPLs, tests and lint.")
        for path in toolchain_paths()
    ),
    *(
        vis.Grant(
            path,
            "read_write",
            reason="Share the uv, Poetry and pip caches with the person's own tools.",
        )
        for path in grantable(cache_paths(), existing=False)
    ),
]


vis.register_extension(
    vis.Extension(
        name="vis-lang-python",
        description="Python tools: ruff formatting and lint, pytest runs and a managed project REPL.",
        version="1.15.3",
        alias="py",
        symbols=[
            vis.Symbol(
                PythonTools(
                    workspace_root=vis.workspace_root,
                    python_environment=vis.python_environment,
                ),
                name="py",
            )
        ],
        prompt=PROMPT,
        op_hooks=[
            vis.OpHook(["patch"], GUARD.before_patch),
            vis.OpHook(["python_execution"], GUARD.before_block),
            vis.OpHook(["patch", "python_execution"], GUARD.after_edit, phase="after"),
        ],
        ctx=GUARD.ctx,
        grants=GRANTS,
    )
)
