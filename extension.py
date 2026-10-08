"""Vis entrypoint. The tools themselves live in vis_lang_python."""

from typing import Literal

import blockether.vis.extension as vis
from vis_lang_interface import presentation, prompt
from vis_lang_interface.syntax import SyntaxGuard

from vis_lang_python.repair import repair_source
from vis_lang_python.tools import SYNTAX_SUFFIXES, PythonTools, _check_syntax

# The tags that this extension binds. Older SDKs do not export `vis.SymbolTag`,
# so this module keeps its own tag type.
_Tag = Literal["observation", "mutation", "verification"]


def _bind(
    name, label, build, *, tag: _Tag = "observation", show_start=True, describe=None
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


def _knows(tag: _Tag) -> bool:
    """Whether this Vis host accepts `tag`. Older hosts refuse `verification`."""
    try:
        vis.method(tag=tag)
    except ValueError:
        return False
    return True


# Lint and test runs check work; a host that lacks the tag records them as reads.
_CHECK: _Tag = "verification" if _knows("verification") else "observation"


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
    tag=_CHECK,
)
_bind(
    "run_tests",
    "Run Python tests",
    lambda result: presentation.test_presentation("Run Python tests", result),
    tag=_CHECK,
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
        " not. `py.repl_eval` needs that interpreter started first.",
        "`py.repl_eval` runs nothing from code that does not parse. It evaluates a safe repair, such"
        " as a missing closer, and lists it in `repairs`. With no safe repair, it reports where the"
        " code stops parsing.",
        "`py.run_tests` runs pytest with the same interpreter and needs no REPL. `py.format_code`"
        " and `py.lint_code` run ruff.",
        "Edit hooks repair structural mistakes in Python files locally and validate the result without"
        " execution. Before a patch writes, its repair stays in the changed lines. Unrepairable breaking"
        " patches write nothing.",
        "Vis repairs each Python block itself before it runs; this extension does not change blocks."
        " After a block, the hooks check the changed Python files and can repair them. A block is not"
        " transactional. Repairs and unresolved errors appear in `python_syntax_repairs` and"
        " `python_syntax_errors` in the session context.",
    ),
)

GUARD = SyntaxGuard("python", SYNTAX_SUFFIXES, _check_syntax, repair=repair_source)


vis.register_extension(
    vis.Extension(
        name="vis-lang-python",
        description="Python tools: ruff formatting and lint, pytest runs and a managed project REPL.",
        version="1.10.1",
        alias="py",
        symbols=[vis.Symbol(PythonTools(workspace_root=vis.workspace_root), name="py")],
        prompt=PROMPT,
        op_hooks=[
            vis.OpHook(["patch"], GUARD.before_patch),
            vis.OpHook(["python_execution"], GUARD.before_block),
            vis.OpHook(["patch", "python_execution"], GUARD.after_edit, phase="after"),
        ],
        ctx=GUARD.ctx,
    )
)
