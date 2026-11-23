"""A Python syntax check that compiles again only the changed top-level statements.

A module is a list of top-level statements, and the tokenizer starts again at
each statement that begins a line. When a file compiled clean before, the check
compiles only the statements from the first to the last changed one, with the
module's `__future__` flags. Each error compiles the whole file, so an error is
always the compiler's own verdict. So does each case where one statement can
change the verdict on another: a `global` statement outside a function, a
`__future__` import after the first change, and a line break other than a newline.

A statement in the body of a top-level class compiles under a copy of the class
header, so that name mangling and type parameters stay the same.
"""

from __future__ import annotations
import __future__

import ast
import bisect
import re
import threading
from dataclasses import dataclass

FUTURE_FLAGS = 0
for _name in __future__.all_feature_names:
    FUTURE_FLAGS |= getattr(__future__, _name).compiler_flag
_NEWLINE = re.compile("\n")


@dataclass(frozen=True)
class Compiled:
    """One source as `check` compiled it, to compile a later version faster.

    `error` is `(line, offset, message)` of the compiler's SyntaxError, or None.
    `flags` holds the module's `__future__` flags. `starts` holds the offset of
    each line where a top-level statement, or a statement in the body of a
    top-level class, begins. For each start, `owners` holds None for a top-level
    statement, else `(class, body)`: the offsets of its class and of the first
    line of the class body. Both are None until a check needs them.
    """

    text: str
    error: tuple[int, int, str] | None
    flags: int = 0
    starts: tuple[int, ...] | None = None
    owners: tuple[tuple[int, int] | None, ...] | None = None


def check(text, path, previous=None):
    """Compile `text` without running it, from the `previous` check of its file."""
    if previous is not None and previous.error is None:
        found = _changes(text, path, previous)
        if found is not None:
            return found
    try:
        code = compile(text, path, "exec", dont_inherit=True)
    except SyntaxError as error:
        return Compiled(text, (error.lineno or 0, error.offset or 0, error.msg))
    return Compiled(text, None, code.co_flags & FUTURE_FLAGS)


def _changes(text, path, previous):
    """`previous` updated by compiling the changed statements, or None to compile all."""
    old = previous.text
    same = _same_start(old, text)
    if same == len(old) == len(text):
        return previous
    if "\r" in text or "\r" in old:
        return None
    starts, owners = previous.starts, previous.owners
    if starts is None:
        tree = ast.parse(old, path)
        if _is_global(tree.body):
            return None
        starts, owners = _starts(tree, old)
    # The statement before the change too: an indented change can extend its body.
    index = max(0, bisect.bisect_right(starts, same - 1) - 1)
    owner = owners[index]
    if owner is not None and same <= _indent_end(old, starts[index]):
        # A changed indent can move the statement out of its class.
        index = bisect.bisect_left(starts, owner[0])
        owner = None
    first = starts[index]
    if text.find("__future__", first) != -1:
        return None
    tail = _same_end(old, text, min(len(old), len(text)) - same)
    shift = len(text) - len(old)
    after = bisect.bisect_left(starts, len(old) - tail)
    while True:
        end = (starts[after] if after < len(starts) else len(old)) + shift
        # The change can put text before the first unchanged statement on its line,
        # and the unchanged rest must continue the block where the change ends.
        if after < len(starts) and (
            text[end - 1] != "\n" or owners[after] not in (None, owner)
        ):
            after += 1
        else:
            break
    if 2 * (end - first) > len(text):
        return None
    header = old[owner[0] : owner[1]] if owner else ""
    source = header + text[first:end]
    flags = previous.flags
    try:
        tree = compile(source, path, "exec", flags | ast.PyCF_ONLY_AST, True)
        compile(tree, path, "exec", flags, True)
    except SyntaxError:
        return None
    if _is_global(tree.body):
        return None
    if owner and after < len(starts) and owners[after] == owner and len(tree.body) > 1:
        return None
    cut = len(header)

    def moved(offset):
        return offset - cut + first

    found, owned = _starts(tree, source)
    kept = bisect.bisect_left(starts, first)
    unchanged = starts[after] if after < len(starts) else len(old)
    return Compiled(
        text,
        None,
        flags,
        (
            *starts[:kept],
            *(moved(start) for start in found if start >= cut),
            *(start + shift for start in starts[after:]),
        ),
        (
            *owners[:kept],
            *(
                owner if one == (0, cut) else one and (moved(one[0]), moved(one[1]))
                for start, one in zip(found, owned)
                if start >= cut
            ),
            *(
                one
                and (one if one[0] < unchanged else (one[0] + shift, one[1] + shift))
                for one in owners[after:]
            ),
        ),
    )


def _starts(tree, source):
    """The starts and owners of `source`, as `Compiled` holds them, from its `tree`.

    The start of `source` always counts. A statement after a semicolon shares
    its line with the one before, so its line does not count.
    """
    lines = [0, *(m.end() for m in _NEWLINE.finditer(source))]
    starts, owners = [0], [None]

    def add(nodes, owner):
        ended = 0
        for node in nodes:
            line = _first_line(node)
            if line > ended and lines[line - 1] > starts[-1]:
                starts.append(lines[line - 1])
                owners.append(owner)
                if owner is None and isinstance(node, ast.ClassDef):
                    body = lines[_first_line(node.body[0]) - 1]
                    # A class with its body on the header line has no own starts.
                    if body > starts[-1]:
                        add(node.body, (starts[-1], body))
            ended = node.end_lineno or node.lineno

    add(tree.body, None)
    return tuple(starts), tuple(owners)


def _indent_end(text, start):
    """The offset after the indent of the line that begins at `start`."""
    end = start
    while end < len(text) and text[end] in " \t\f":
        end += 1
    return end


def _first_line(node):
    return min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", ()))])


def _is_global(nodes):
    """True when `nodes` hold a `global` statement outside a function.

    Such a statement changes the verdict on other statements of its module or class.
    """
    for node in nodes:
        if isinstance(node, ast.Global):
            return True
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                if _is_global(getattr(node, field, ())):
                    return True
    return False


def _same_start(a, b):
    """How many characters `a` and `b` share at their start."""
    low, high = 0, min(len(a), len(b))
    # Each step compares half of what is left, so all steps copy one length.
    while low < high:
        middle = (low + high + 1) // 2
        if a[low:middle] == b[low:middle]:
            low = middle
        else:
            high = middle - 1
    return low


def _same_end(a, b, most):
    """How many characters, at most `most`, `a` and `b` share at their end."""
    low, high = 0, most
    while low < high:
        middle = (low + high + 1) // 2
        if a[len(a) - middle : len(a) - low] == b[len(b) - middle : len(b) - low]:
            low = middle
        else:
            high = middle - 1
    return low


class Checks:
    """The last checks of each file, so that the next check compiles only its changes.

    For each file it keeps the last check and, when that one found an error,
    the last clean check too. A fix usually brings a file back near its last
    clean version, so a check after a fix starts from that one.
    """

    def __init__(self, most_characters):
        self.most_characters = most_characters
        self.characters = 0
        self.checks = {}
        self.lock = threading.Lock()

    def check(self, key, text, path):
        """`check` of `text`, from the last check of `key` that fits best."""
        with self.lock:
            kept = self.checks.get(key, ())
        clean = next((one for one in reversed(kept) if one.error is None), None)
        found = check(text, path, clean or (kept[-1] if kept else None))
        if found.error is None:
            clean = found
        with self.lock:
            # The oldest file goes first. Another thread can have kept one too.
            self._drop(key)
            self.checks[key] = (
                (clean, found) if clean is not None and clean is not found else (found,)
            )
            self.characters += sum(len(one.text) for one in self.checks[key])
            while self.characters > self.most_characters and self.checks:
                self._drop(next(iter(self.checks)))
        return found

    def _drop(self, key):
        for one in self.checks.pop(key, ()):
            self.characters -= len(one.text)
