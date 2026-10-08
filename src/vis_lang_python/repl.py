"""The managed Python REPL this extension owns.

`py.repl_start`, `py.repl_status`, `py.repl_stop` and `py.repl_eval` reach this
module. It launches the project's own interpreter as a persistent child: uv,
Poetry, a `.venv` or `python3`. The child runs a small line-framed eval server.
One JSON request goes in per line, and one JSON answer comes out per line.
Globals live between evaluations. That is what makes it a REPL and not a series
of scripts.

There is one runtime per project directory. This extension owns it, and the
workspace jail confines it like everything else that a language tool starts.
The runtime holds its own channels open and runs under a shell id, so it
outlives a sandbox restart: the next Python process attaches to it, with its
globals. Only `stop` and the end of the session end it.
"""

from __future__ import annotations

import atexit
import hashlib
import itertools
import json
import os
import secrets
import shlex
import shutil
import threading
import time
from pathlib import Path

from vis_lang_interface import RuntimeGone, runtime
from vis_lang_interface.process import shell_call

from vis_lang_python import caches

DRIVER = r"""import sys, json, io, ast, contextlib, pprint, traceback

_G = {'__name__': '__vis_repl__'}


def _repr(value):
    try:
        s = repr(value)
        # A long container reads better laid out by pprint, one item per line;
        # text stays one literal and a huge value keeps its plain repr.
        if 88 < len(s) <= 8000 and not isinstance(value, (str, bytes, bytearray)):
            s = pprint.pformat(value, width=88, sort_dicts=False)
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


def _compiled(code):
    block = ast.parse(code, '<repl>', 'exec')
    body = block.body
    if body and isinstance(body[-1], ast.Expr):
        pre = compile(ast.Module(body[:-1], []), '<repl>', 'exec')
        return pre, compile(ast.Expression(body[-1].value), '<repl>', 'eval')
    return compile(block, '<repl>', 'exec'), None


def _run(code):
    out = io.StringIO()
    err = io.StringIO()
    value = None
    ok = True
    exc = None
    syntax = None
    has_value = False
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            # Compile all of the code before any of it runs. Code that does not
            # parse runs nothing, and the answer says where it stops.
            try:
                pre, last = _compiled(code)
            except SyntaxError as e:
                syntax = {'line': e.lineno or 0, 'column': e.offset or 0,
                          'message': e.msg}
                raise
            exec(pre, _G)
            if last is not None:
                has_value = True
                value = eval(last, _G)
    except BaseException as e:
        ok = False
        if syntax is None:
            exc = traceback.format_exc()
        else:
            exc = ''.join(traceback.format_exception_only(type(e), e))
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
            'exc': exc,
            'syntax': syntax}


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
        if 'id' in req:
            res['id'] = req['id']
        sys.stdout.write(json.dumps(res) + '\n')
        sys.stdout.flush()


_main()
"""

STDERR_TAIL_LINES = 40
"""How many of the child's last stderr lines a failed start hands back."""

LOADER = (
    "import os, sys; "
    "p = os.path.join(os.environ['VIS_LANG_RENDEZVOUS'], 'driver.py'); "
    "sys.argv = [p]; "
    "exec(compile(open(p, encoding='utf-8').read(), p, 'exec'), "
    "{'__name__': '__main__', '__file__': p})"
)
"""Runs the driver from the runtime's rendezvous. The command line stays the same
for every start in one directory, so a later process finds the runtime by it."""

PING_TIMEOUT_S = 5.0
DEFAULT_EVAL_TIMEOUT_S = 30.0

_REPLS: dict[str, _Repl] = {}
_REPLS_LOCK = threading.RLock()


def _text(value) -> str:
    return "" if value is None else str(value)


def abbreviate_home(path: str) -> str:
    home = os.path.expanduser("~")
    if home and path.startswith(home):
        return "~" + path[len(home) :]
    return path


def _project_dir(options) -> str:
    """The real path of the directory that this call names. `tools.session_root`
    resolves a relative `cwd` against the session before it reaches here. A
    direct caller gets the process directory."""
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

    Never RESOLVED: `.venv/bin/python3` is a symlink chain that ends at the base
    installation. Following it leaves the virtualenv. Then `sys.prefix` becomes
    the base prefix and `pyvenv.cfg` is never read. The run fails with
    `No module named pytest`, while the same suite passes under
    `.venv/bin/python`.
    """
    for venv in (".venv", "venv"):
        for name in ("bin/python", "bin/python3"):
            candidate = root / venv / name
            if candidate.is_file():
                return str(candidate)
    return None


def _is_uv_project(root: Path) -> bool:
    """A `uv.lock`, or a real `[tool.uv]` table in `pyproject.toml`, read as
    TOML and never as a substring search. `[tool.uvicorn]`, a commented-out
    `[tool.uv]` or a description that only mentions one is not a uv project.
    Picking `uv run python` for those launches the wrong interpreter."""
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
    """The argv PREFIX that launches a project-aware Python in `cwd`. The first
    match wins, in this order: uv, Poetry, a project virtualenv, then the system
    interpreter. uv needs `uv.lock` or `[tool.uv]`, plus `uv` on PATH. Poetry
    needs `poetry.lock`, plus `poetry` on PATH."""
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
        # A kept interpreter can still hold answers to an earlier Python
        # process's calls, so every id carries a prefix of this object's own.
        self.prefix = secrets.token_hex(4)
        self.ids = itertools.count(1)
        # The driver evaluates one request at a time: a call's timeout starts
        # when its turn comes.
        self.lock = threading.Lock()

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
        # The loader the interpreter runs is elided: this argv rides into
        # `status`, the resource registry and the footer.
        return [*self.cmd[:-2], "<vis python driver>"]

    def request(self, payload: dict, timeout_s: float) -> dict:
        try:
            with self.lock:
                wanted = f"{self.prefix}-{next(self.ids)}"
                return self.live.request(
                    dict(payload, id=wanted), timeout_s, wants=wanted
                )
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

    def detach(self) -> None:
        """Leave the interpreter and its globals running for the next process."""
        self.live.detach()


def shell_id(cwd: str) -> str:
    """The shell id of the REPL for `cwd`.

    The id keeps the REPL across a sandbox restart: the next process that loads
    this extension attaches to it, with its globals.
    """
    digest = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:12]
    return f"vis-lang-python-{digest}"


def _command(cwd: str) -> list[str]:
    return [*detect_command(cwd), "-c", LOADER]


def _kept_fingerprint(command) -> dict:
    """The environment fingerprint that the start of a kept REPL placed."""
    try:
        words = shlex.split(str(command or ""))
    except ValueError:
        return {}
    for word in words:
        if word.startswith("3<>"):
            place = os.path.join(os.path.dirname(word[3:]), "env.json")
            try:
                with open(place, encoding="utf-8") as handle:
                    value = json.load(handle)
            except (OSError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}
    return {}


def _kept(cwd: str) -> _Repl | None:
    """The REPL that an earlier Python process left running for `cwd`, or None.

    It never starts an interpreter, and it never stops one that runs another
    command: the globals of a live REPL are the user's work.
    """
    sid = shell_id(cwd)
    try:
        shell = shell_call()({"op": "logs", "id": sid, "offset": -1})
    except Exception:
        return None
    if str(shell.get("status")) != "running":
        return None
    cmd = _command(cwd)
    if shlex.join(cmd) not in str(shell.get("command") or ""):
        return None
    try:
        live = runtime.start(
            cmd, cwd=cwd, read_write=caches.granted_paths(), shell_id=sid
        )
    except Exception:
        return None
    if live.pid != shell.get("pid"):
        # The kept rendezvous was gone, so a new interpreter started without
        # its driver. That one is not the user's REPL.
        live.stop()
        return None
    return _Repl(live, cwd, cmd, _kept_fingerprint(shell.get("command")))


def _live(cwd: str) -> _Repl | None:
    with _REPLS_LOCK:
        repl = _REPLS.get(cwd)
        if repl is not None and not repl.is_alive():
            _REPLS.pop(cwd, None)
            return None
        if repl is None:
            repl = _kept(cwd)
            if repl is not None:
                _REPLS[cwd] = repl
        return repl


def _forget(cwd: str) -> _Repl | None:
    with _REPLS_LOCK:
        return _REPLS.pop(cwd, None)


def _status(cwd: str, repl: _Repl | None) -> dict:
    """The lifecycle view that every language answers. A key appears only where
    it MEANS something, so a REPL that is down has no pid and no command."""
    answer: dict[str, object] = {
        "result": "status",
        "cwd": cwd,
        "status": "up" if repl else "down",
    }
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

    A live REPL is NEVER silently replaced: its globals ARE the session's work.
    So a second start answers `already-running`. A start succeeds only after the
    child answers its protocol ping. A failed child is never reported as a
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
        meeting.place("driver.py", DRIVER)
        # A later process reads the fingerprint back when it attaches.
        meeting.place(
            "env.json", json.dumps(dict(options.get("env_fingerprint") or {}))
        )
        cmd = _command(cwd)
        live = runtime.start(
            cmd,
            cwd=cwd,
            env=_child_environment(options.get("env")),
            meeting=meeting,
            read_write=caches.granted_paths(),
            shell_id=shell_id(cwd),
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
    repl = _live(cwd)
    _forget(cwd)
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
    across calls. Answers `{ok, out, err, value, data, type, exc}` plus the
    `code` that ran, which the REPL op-card shows. `value` is the repr of the
    last expression, and `data` is its JSON-safe structured view. `type` is the
    class name."""
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


def detach_all() -> None:
    """Leave every REPL running, kept for the next Python process.

    A sandbox restart ends this Python process. The interpreters live on with
    their globals, and the next call attaches to them. `stop` and the end of
    the session stop them.
    """
    with _REPLS_LOCK:
        live = list(_REPLS.values())
        _REPLS.clear()
    for repl in live:
        repl.detach()


atexit.register(detach_all)
