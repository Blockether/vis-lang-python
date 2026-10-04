"""Vis entrypoint. The tools themselves live in vis_lang_python."""

import ast

import blockether.vis.extension as vis
from vis_lang_interface import presentation, prompt
from vis_lang_interface.syntax import SyntaxGuard

from vis_lang_python.repair import repair_source
from vis_lang_python.tools import SYNTAX_SUFFIXES, PythonTools, _check_syntax


def _bind(name, label, build, *, tag="observation", show_start=True, describe=None):
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


def _knows(tag):
    """Whether this Vis host accepts `tag`. Older hosts refuse `verification`."""
    try:
        vis.method(tag=tag)
    except ValueError:
        return False
    return True


# Lint and test runs check work; a host that lacks the tag records them as reads.
_CHECK = "verification" if _knows("verification") else "observation"


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
        "`py.run_tests` runs pytest with the same interpreter and needs no REPL. `py.format_code`"
        " and `py.lint_code` run ruff.",
        "Edit hooks repair structural mistakes in Python locally and validate the result without execution."
        " Before a patch writes, its repair stays in the changed lines. Unrepairable breaking patches write nothing.",
        "Python blocks are repaired before execution, including top-level await. Changed files can be repaired"
        " after the block. A block is not transactional. Repairs and unresolved errors appear in"
        " `python_syntax_repairs` and `python_syntax_errors` in the session context.",
    ),
)

GUARD = SyntaxGuard("python", SYNTAX_SUFFIXES, _check_syntax, repair=repair_source)


def _block_parses(source):
    """Validate a sandbox block, including top-level await, without execution."""
    try:
        tree = ast.parse(source, filename="<python_execution>")
        compile(
            tree,
            "<python_execution>",
            "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            dont_inherit=True,
        )
    except (SyntaxError, ValueError):
        return False
    return True


def _before_block(call):
    """Record the file baseline, then repair the block before it can execute."""
    GUARD.before_block(call)
    args = call.get("args")
    if (
        not isinstance(args, (list, tuple))
        or len(args) != 1
        or not isinstance(args[0], dict)
    ):
        return None
    source = args[0].get("code")
    if not isinstance(source, str):
        return None
    candidate = repair_source(source, parses_clean=_block_parses)
    if candidate is not None:
        return {
            "marker": "repair",
            "source": candidate.source,
            "notes": list(candidate.notes),
        }
    return None


vis.register_extension(
    vis.Extension(
        name="vis-lang-python",
        description="Python tools: ruff formatting and lint, pytest runs and a managed project REPL.",
        version="1.7.0",
        alias="py",
        symbols=[vis.Symbol(PythonTools(workspace_root=vis.workspace_root), name="py")],
        prompt=PROMPT,
        op_hooks=[
            vis.OpHook(["patch"], GUARD.before_patch),
            vis.OpHook(["python_execution"], _before_block),
            vis.OpHook(["patch", "python_execution"], GUARD.after_edit, phase="after"),
        ],
        ctx=GUARD.ctx,
    )
)
