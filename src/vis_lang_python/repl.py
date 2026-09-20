"""The managed Python REPL this extension owns.

`py.repl_start`, `py.repl_status`, `py.repl_stop` and `py.repl_eval` reach this
module. It launches the project's own interpreter — uv, Poetry, a `.venv` or
`python3` — as a persistent child running a small line-framed eval server: one
JSON request per line in, one JSON answer per line out, with globals that live
between evaluations, which is what makes it a REPL rather than a series of
scripts.

One runtime per project directory, owned by this extension and confined by the
workspace jail like everything else a language tool starts: closing the channel
it reads is what ends it, so an extension that goes away never leaves an
interpreter behind.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from vis_lang_interface import RuntimeGone, runtime

DRIVER = r"""import sys, json, io, ast, contextlib, traceback

_G = {'__name__': '__vis_repl__'}


def _repr(value):
    try:
        s = repr(value)
    except Exception as ex:
        s = '<unreprable ' + type(value).__name__ + ': ' + str(ex) + '>'
    return s[:8000]


def _safe(o, depth=0):
    # Make a REAL Python object representable as JSON-safe nested data so the
    # model can read its actual fields, not just an opaque repr. Handles
    # primitives, list/tuple/set, dict, namedtuples (_asdict), numpy/pandas
    # (tolist/to_dict), and plain objects (__dict__, tagged with __type__);
    # anything else degrades to repr. Bounded by depth + per-collection cap.
    if depth > 6:
        return repr(o)
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, (list, tuple)):
        return [_safe(x, depth + 1) for x in list(o)[:1000]]
    if isinstance(o, (set, frozenset)):
        return [_safe(x, depth + 1) for x in list(o)[:1000]]
    if isinstance(o, dict):
        return {str(k): _safe(v, depth + 1) for k, v in list(o.items())[:1000]}
    for attr in ('_asdict', 'tolist', 'to_dict'):
        f = getattr(o, attr, None)
        if callable(f):
            try:
                return _safe(f(), depth + 1)
            except Exception:
                pass
    dd = getattr(o, '__dict__', None)
    if isinstance(dd, dict) and dd:
        out = {'__type__': type(o).__name__}
        for k, v in list(dd.items())[:1000]:
            out[str(k)] = _safe(v, depth + 1)
        return out
    # OPAQUE object — can't be turned into data (file handle, generator, model,
    # connection, C-extension object). It is NOT lost: it stays LIVE in the
    # REPL's globals, so bind it to a name (`m = load_model()`) and keep calling
    # it in later evals. Here we just describe it — type, repr, and (top level)
    # its public attributes/methods — so the model knows what it can do with it.
    info = {'__type__': type(o).__name__, '__repr__': _repr(o), '__opaque__': True}
    if depth == 0:
        attrs = [n for n in dir(o) if not n.startswith('_')][:50]
        if attrs:
            info['__attrs__'] = attrs
    return info


def _run(code):
    out = io.StringIO()
    err = io.StringIO()
    value = None
    ok = True
    exc = None
    has_value = False
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            block = ast.parse(code, mode='exec')
            body = block.body
            if body and isinstance(body[-1], ast.Expr):
                has_value = True
                pre = ast.Module(body[:-1], [])
                last = ast.Expression(body[-1].value)
                exec(compile(pre, '<repl>', 'exec'), _G)
                value = eval(compile(last, '<repl>', 'eval'), _G)
            else:
                exec(compile(block, '<repl>', 'exec'), _G)
    except BaseException:
        ok = False
        exc = traceback.format_exc()
    has_v = has_value and value is not None
    try:
        data = _safe(value) if has_v else None
    except Exception:
        data = None
    return {'ok': ok,
            'out': out.getvalue(),
            'err': err.getvalue(),
            'value': (_repr(value) if has_v else None),
            'data': data,
            'type': (type(value).__name__ if has_v else None),
            'exc': exc}


def _main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            sys.stdout.write(json.dumps({'ok': False, 'exc': 'bad request'}) + '\n')
            sys.stdout.flush()
            continue
        res = {'ok': True, 'pong': True} if req.get('op') == 'ping' else _run(req.get('code', ''))
        sys.stdout.write(json.dumps(res) + '\n')
        sys.stdout.flush()


_main()
"""

STDERR_TAIL_LINES = 40
"""How many of the child's last stderr lines a failed start hands back."""

PING_TIMEOUT_S = 5.0
DEFAULT_EVAL_TIMEOUT_S = 30.0

_REPLS: dict[str, _Repl] = {}
_REPLS_LOCK = threading.Lock()


def _text(value) -> str:
    return "" if value is None else str(value)


def abbreviate_home(path: str) -> str:
    home = os.path.expanduser("~")
    if home and path.startswith(home):
        return "~" + path[len(home) :]
    return path


def _project_dir(options) -> str:
    """The canonical directory this call names. Vis resolves `cwd` before the
    call; a direct caller gets the process directory."""
    raw = options.get("cwd") if isinstance(options, dict) else None
    return os.path.realpath(os.path.expanduser(_text(raw) or os.getcwd()))


def _repl_id(options, cwd: str) -> str:
    for key in ("id", "repl_id"):
        value = options.get(key) if isinstance(options, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "pyrepl:" + cwd


def _venv_python(root: Path) -> str | None:
    """ABSOLUTE path of a project-local virtualenv's interpreter, or None.

    Never RESOLVED: `.venv/bin/python3` is a symlink chain ending at the base
    installation, and following it walks out of the virtualenv — `sys.prefix`
    becomes the base prefix, `pyvenv.cfg` is never read, and the run dies with
    `No module named pytest` while the same suite passes under `.venv/bin/python`.
    """
    for venv in (".venv", "venv"):
        for name in ("bin/python", "bin/python3"):
            candidate = root / venv / name
            if candidate.is_file():
                return str(candidate)
    return None


def _is_uv_project(root: Path) -> bool:
    """A `uv.lock`, or a real `[tool.uv]` table in `pyproject.toml` — read as
    TOML, never as substring soup: `[tool.uvicorn]`, a commented-out `[tool.uv]`
    and a description that merely mentions one are not uv projects, and picking
    `uv run python` for them launches the wrong interpreter."""
    if (root / "uv.lock").is_file():
        return True
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        import tomllib

        tool = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool")
    except Exception:
        return False
    return isinstance(tool, dict) and isinstance(tool.get("uv"), dict)


def detect_command(cwd: str) -> list[str]:
    """The argv PREFIX that launches a project-aware Python in `cwd`, first hit
    winning: uv (`uv.lock` / `[tool.uv]` plus `uv` on PATH), Poetry
    (`poetry.lock` plus `poetry` on PATH), a project virtualenv, then the
    system interpreter."""
    root = Path(cwd)
    if _is_uv_project(root) and shutil.which("uv"):
        return ["uv", "run", "python"]
    if (root / "poetry.lock").is_file() and shutil.which("poetry"):
        return ["poetry", "run", "python"]
    venv = _venv_python(root)
    if venv:
        return [venv]
    return ["python3" if shutil.which("python3") else "python"]


def _child_environment(values) -> dict:
    """One call's environment DELTA over what the runtime inherits — this
    worker's own environment, the project's `.env` and `environment:`
    declarations included. A name mapped to None is UNSET for the runtime."""
    delta = {}
    for name, value in (values or {}).items():
        delta[str(name)] = None if value is None else str(value)
    return delta


class ReplError(RuntimeError):
    """A REPL call that cannot be served — down, timed out, or dead."""


class _Repl:
    """One interpreter runtime and the framed conversation with it."""

    def __init__(self, live, cwd: str, cmd: list[str], env_fingerprint):
        self.live = live
        self.cwd = cwd
        self.cmd = cmd
        self.env_fingerprint = dict(env_fingerprint or {})
        self.started_at = time.time()

    @property
    def pid(self) -> int:
        return self.live.pid

    def is_alive(self) -> bool:
        return self.live.is_running

    def exit_code(self):
        return self.live.exit_code

    def stderr_tail(self) -> list[str]:
        # Whatever the interpreter says about itself stays on its own log, which
        # this reads back the moment the runtime is found dead. The pause is for
        # the loaded machine that has not written it yet: without it a dead
        # launch can answer with no tail at all, losing the one line that
        # explains it.
        deadline = time.time() + 0.5
        tail = self.live.log_tail(STDERR_TAIL_LINES)
        while not tail and time.time() < deadline:
            time.sleep(0.01)
            tail = self.live.log_tail(STDERR_TAIL_LINES)
        return tail

    def displayed_cmd(self) -> list[str]:
        # The driver the interpreter runs is elided: this argv rides into
        # `status`, the resource registry and the footer.
        return [*self.cmd[:-1], "<vis python driver>"]

    def request(self, payload: dict, timeout_s: float) -> dict:
        try:
            return self.live.request(payload, timeout_s)
        except RuntimeGone as ex:
            self.kill()
            raise ReplError(
                "Python REPL closed the connection; start it again with "
                f"py.repl_start(). ({ex})"
            ) from ex
        except TimeoutError:
            raise ReplError(
                f"Python eval timed out after {int(timeout_s * 1000)}ms"
            ) from None

    def kill(self) -> None:
        self.live.stop()


def _live(cwd: str) -> _Repl | None:
    with _REPLS_LOCK:
        repl = _REPLS.get(cwd)
        if repl is not None and not repl.is_alive():
            _REPLS.pop(cwd, None)
            return None
        return repl


def _forget(cwd: str) -> _Repl | None:
    with _REPLS_LOCK:
        return _REPLS.pop(cwd, None)


def _status(cwd: str, repl: _Repl | None) -> dict:
    """The lifecycle view every language answers: a key appears only where it
    MEANS something, so a REPL that is down has no pid and no command."""
    answer = {"result": "status", "cwd": cwd, "status": "up" if repl else "down"}
    if repl:
        answer["running"] = True
        answer["pid"] = repl.pid
        answer["cmd"] = repl.displayed_cmd()
        if repl.env_fingerprint:
            # By NAME and digest only — never a value: this rides into a result,
            # a log and the transcript.
            answer["env"] = dict(repl.env_fingerprint)
    return answer


def status(options=None) -> dict:
    options = options or {}
    cwd = _project_dir(options)
    answer = _status(cwd, _live(cwd))
    answer["id"] = _repl_id(options, cwd)
    return answer


def start(options=None) -> dict:
    """Start this worker's Python REPL for `cwd`, or reuse the live one.

    A live REPL is NEVER silently replaced: its globals ARE the session's work,
    so a second start answers `already-running`. A start succeeds only after the
    child answers its protocol ping; a failed child never masquerades as a
    usable REPL.
    """
    options = options or {}
    cwd = _project_dir(options)
    repl_id = _repl_id(options, cwd)
    live = _live(cwd)
    if live:
        answer = _status(cwd, live)
        answer["result"] = "already-running"
        answer["id"] = repl_id
        return answer

    meeting = runtime.Rendezvous("python").open()
    try:
        # The driver is a file next to the runtime's channels, not 17k of source
        # on a command line the shell would have to carry through intact.
        cmd = [*detect_command(cwd), meeting.place("driver.py", DRIVER)]
        live = runtime.start(
            cmd,
            cwd=cwd,
            env=_child_environment(options.get("env")),
            meeting=meeting,
        )
    except BaseException:
        meeting.close()
        raise
    repl = _Repl(live, cwd, cmd, options.get("env_fingerprint"))
    try:
        pong = repl.request({"op": "ping"}, PING_TIMEOUT_S)
        if pong.get("pong") is not True:
            raise ReplError("Python REPL did not acknowledge its startup ping")
    except Exception as ex:
        exit_code = repl.exit_code()
        tail = repl.stderr_tail()
        repl.kill()
        answer = {
            "result": "failed",
            "status": "failed",
            "id": repl_id,
            "pid": repl.pid,
            "cmd": repl.displayed_cmd(),
            "cwd": cwd,
            "message": f"Python REPL failed its startup handshake: {ex}",
        }
        if exit_code is not None:
            answer["exit"] = exit_code
        if tail:
            answer["log_tail"] = tail
        return answer

    with _REPLS_LOCK:
        _REPLS[cwd] = repl
    answer = _status(cwd, repl)
    answer["result"] = "started"
    answer["id"] = repl_id
    return answer


def stop(options=None) -> dict:
    """Stop this worker's interpreter for `cwd`. No-op-safe: with nothing
    managed the result says `not-managed`, not a stop that never happened."""
    options = options or {}
    cwd = _project_dir(options)
    repl = _forget(cwd)
    if repl:
        repl.kill()
    return {
        "result": "stopped" if repl else "not-managed",
        "cwd": cwd,
        "status": "down",
        "id": _repl_id(options, cwd),
    }


def evaluate(options) -> dict:
    """Evaluate `code` in this worker's REPL for `cwd`, with globals persistent
    across calls. Answers `{ok, out, err, value, data, type, exc}` — `value` is
    the last expression's repr, `data` its JSON-safe structured view, `type` the
    class name — plus the `code` that ran, which the REPL op-card shows."""
    options = {"code": options} if isinstance(options, str) else (options or {})
    code = options.get("code")
    if code is None:
        code = options.get("source")
    if code is None:
        raise ValueError('repl_eval(python) expects a code string or {"code": ...}')
    cwd = _project_dir(options)
    repl = _live(cwd)
    if repl is None:
        shown = abbreviate_home(cwd)
        raise ReplError(
            f"Python REPL is not up for {shown}; "
            f'call py.repl_start(cwd="{shown}") first'
        )
    timeout_ms = options.get("timeout_ms")
    timeout_s = (float(timeout_ms) / 1000.0) if timeout_ms else DEFAULT_EVAL_TIMEOUT_S
    answer = repl.request({"code": _text(code)}, timeout_s)
    answer["code"] = _text(code)
    return answer
