"""Scan Python delimiters without importing or executing the source.

Ported from clj-parinferish. Copyright (c) 2026 Blockether, MIT License.
The repair budget bounds full rescans. A scan does not validate Python grammar.
"""

from bisect import bisect_right
from dataclasses import dataclass
from keyword import iskeyword

(
    UNTERMINATED_STRING,
    UNTERMINATED_TRIPLE,
    UNCLOSED,
    UNMATCHED,
    MISMATCHED,
    STRAY_BACKSLASH,
    TYPOGRAPHIC_QUOTE,
    FSTRING_BRACE,
    SEMICOLON,
    MARKER,
    COMPOUND_AFTER_SEMICOLON,
    LOST_NEWLINE,
    FSTRING_FIELD,
    INVALID_ESCAPE,
    TEXT_AFTER_STRING,
) = range(15)

F_STRING, F_FIELD, F_SPEC = range(3)
FORMAT, RAW, BYTES = 1, 2, 4
CLOSERS = {"(": ")", "[": "]", "{": "}"}
COMPOUND = ("def", "class", "with", "for", "while", "if", "try", "async")
STATEMENTS = frozenset(
    (
        "def",
        "class",
        "return",
        "import",
        "from",
        "with",
        "try",
        "except",
        "finally",
        "elif",
        "while",
        "raise",
        "pass",
        "break",
        "continue",
        "del",
        "global",
        "nonlocal",
        "assert",
        "async",
        "await",
    )
)
EXPRESSIONS = frozenset(
    (
        "not",
        "and",
        "or",
        "in",
        "is",
        "lambda",
        "yield",
        "None",
        "True",
        "False",
    )
)


def ident_start(char: str) -> bool:
    return char == "_" or char.isalpha() and ord(char) <= 0xFFFF


def ident_part(char: str) -> bool:
    return ident_start(char) or char.isdecimal() and ord(char) <= 0xFFFF


def prefix(text: str) -> int:
    value = text.lower()
    if value not in {"r", "u", "f", "t", "b", "rf", "fr", "rt", "tr", "rb", "br"}:
        return -1
    return (
        (FORMAT if "f" in value or "t" in value else 0)
        | (RAW if "r" in value else 0)
        | (BYTES if "b" in value else 0)
    )


@dataclass(frozen=True)
class Problem:
    kind: int
    pos: int
    at: int = -1
    opened: tuple[int, ...] = ()


@dataclass
class Frame:
    kind: int
    start: int
    base: int
    quote: int = 0
    triple: bool = False
    raw: bool = False
    bad: bool = False


class Scanner:
    """Keep lexical problems, open delimiters and statement starts for one source."""

    def __init__(self, source: str):
        self.s = source
        self.n = len(source)
        self.line_start = [0] + [i + 1 for i, char in enumerate(source) if char == "\n"]
        self.lines = len(self.line_start)
        self.line_depth = [-1] * self.lines
        self.line_cont = [False] * self.lines
        self.line_comment = [-1] * self.lines
        self.problems: list[Problem] = []
        self.suspects: list[tuple[int, tuple[int, ...]]] = []
        self.ends_in_code = True
        self.stack: list[int] = []
        self.frames: list[Frame] = []
        self.stmt_indent = 0

    def scan(self):
        self.line(0, False)
        lead = len(self.s) - len(self.s.lstrip())
        if lead < self.n and self.s[lead] == ">":
            self.add(MARKER, lead)
        i = 0
        while i < self.n:
            if not self.frames:
                i = self.code(i, 0)
            elif self.frames[-1].kind == F_STRING:
                i = self.literal(i)
            elif self.frames[-1].kind == F_FIELD:
                i = self.code(i, self.frames[-1].base)
            else:
                i = self.spec(i)
        if self.frames:
            frame = self.frames[0]
            self.add(
                UNTERMINATED_TRIPLE if frame.triple else UNTERMINATED_STRING,
                frame.start,
                self.n,
            )
            del self.stack[frame.base :]
            self.frames.clear()
            self.ends_in_code = False
        for pos in self.stack:
            self.add(UNCLOSED, pos)
        return self

    def line_of(self, pos: int) -> int:
        return bisect_right(self.line_start, pos) - 1

    def column(self, pos: int) -> int:
        # Preserve the original engine's UTF-16 column coordinates.
        return (
            len(
                self.s[self.line_start[self.line_of(pos)] : pos].encode(
                    "utf-16-le", "surrogatepass"
                )
            )
            // 2
            + 1
        )

    def line_end(self, line: int) -> int:
        return self.line_start[line + 1] - 1 if line + 1 < self.lines else self.n

    def add(self, kind: int, pos: int, at: int = -1, opened=()):
        self.problems.append(Problem(kind, pos, at, tuple(opened)))

    def window(self):
        return tuple(self.stack[-32:])

    def code(self, i: int, base: int) -> int:
        s, n = self.s, self.n
        char = s[i]
        if char in " \t\f\r":
            return i + 1
        if char == "\n":
            if not self.frames:
                self.line(i + 1, False)
            return i + 1
        if char == "#":
            if not self.frames:
                self.line_comment[self.line_of(i)] = i
            end = s.find("\n", i)
            return end if end >= 0 else n
        if char == "\\":
            j = i + 1
            if j < n and s[j] == "\r":
                j += 1
            if j < n and s[j] == "\n":
                if not self.frames:
                    self.line(j + 1, True)
                return j + 1
            self.add(STRAY_BACKSLASH, i)
            return i + 1
        if char in "'\"":
            return self.touching(i, self.string(i, i, 0))
        if char in CLOSERS:
            self.stack.append(i)
            return i + 1
        if char in ")]}":
            return self.close(i, char, base)
        if char == ":":
            if self.frames and len(self.stack) == base:
                self.frames.append(Frame(F_SPEC, i, len(self.stack)))
            return i + 1
        if char == ";":
            if self.frames:
                self.bad_field()
            elif self.stack:
                self.add(SEMICOLON, i, self.stack[-1], self.window())
                self.stack.clear()
            elif self.compound(i + 1):
                self.add(COMPOUND_AFTER_SEMICOLON, i)
            return i + 1
        if char in "$?`":
            if self.frames and (char != "$" or not ident_part(s[i - 1])):
                self.bad_field()
            return i + 1
        if char in "\u2018\u2019\u201c\u201d":
            self.add(TYPOGRAPHIC_QUOTE, i)
            return i + 1
        if char in "\u00ab\u00bb":
            self.add(MARKER, i)
            return i + 1
        if ident_start(char):
            j = i + 1
            while j < n and ident_part(s[j]):
                j += 1
            if (
                char == "n"
                and not self.frames
                and not self.stack
                and i > 0
                and s[i - 1] in ")]}'\""
                and s[i:j] != "not"
            ):
                self.add(LOST_NEWLINE, i)
            if j < n and j - i <= 2 and s[j] in "'\"":
                flags = prefix(s[i:j])
                if flags >= 0:
                    end = self.string(i, j, flags)
                    return end if flags & FORMAT else self.touching(j, end)
            return j
        if "0" <= char <= "9":
            j = i + 1
            while j < n and (ident_part(s[j]) or s[j] == "."):
                j += 1
            return j
        return i + 1

    def compound(self, pos: int) -> bool:
        while pos < self.n and self.s[pos] in " \t":
            pos += 1
        return any(
            pos + len(word) < self.n
            and self.s.startswith(word, pos)
            and not ident_part(self.s[pos + len(word)])
            for word in COMPOUND
        )

    def string(self, start: int, qp: int, flags: int) -> int:
        s, n = self.s, self.n
        quote = s[qp]
        triple = s.startswith(quote * 3, qp)
        j = qp + (3 if triple else 1)
        if flags & FORMAT:
            self.frames.append(
                Frame(F_STRING, start, len(self.stack), qp, triple, bool(flags & RAW))
            )
            return j
        while j < n:
            char = s[j]
            if char == "\\":
                if (
                    not flags & RAW
                    and j + 1 < n
                    and self.escape_end(j, bool(flags & BYTES)) < 0
                ):
                    self.add(INVALID_ESCAPE, j)
                j += 3 if s.startswith("\r\n", j + 1) else 2
                continue
            if char == quote:
                if not triple:
                    return j + 1
                if s.startswith(quote * 3, j):
                    return j + 3
            elif char == "\n" and not triple:
                self.add(UNTERMINATED_STRING, start, j)
                return j
            j += 1
        self.add(UNTERMINATED_TRIPLE if triple else UNTERMINATED_STRING, start, n)
        self.ends_in_code = False
        return n

    def literal(self, i: int) -> int:
        frame = self.frames[-1]
        char, quote = self.s[i], self.s[frame.quote]
        if char == "\\":
            return self.escape(i, frame.raw)
        if char == quote:
            if not frame.triple:
                return self.end(len(self.frames) - 1, i + 1)
            if self.s.startswith(quote * 3, i):
                return self.end(len(self.frames) - 1, i + 3)
            return i + 1
        if char == "\n" and not frame.triple:
            self.abandon(len(self.frames) - 1, i)
            return i
        if char == "{":
            if self.s.startswith("{{", i):
                return i + 2
            self.frames.append(Frame(F_FIELD, i, len(self.stack)))
            if not self.expression_start(i + 1):
                self.bad_field()
            return i + 1
        if char == "}":
            if self.s.startswith("}}", i):
                return i + 2
            self.add(FSTRING_BRACE, i)
        return i + 1

    def spec(self, i: int) -> int:
        g = len(self.frames) - 1
        while self.frames[g].kind != F_STRING:
            g -= 1
        frame = self.frames[g]
        char, quote = self.s[i], self.s[frame.quote]
        if char == "{":
            self.frames.append(Frame(F_FIELD, i, len(self.stack)))
            return i + 1
        if char == "}":
            del self.frames[-2:]
            return i + 1
        if char == "\\":
            return self.escape(i, frame.raw)
        if char == quote:
            if not frame.triple:
                return self.end(g, i + 1)
            if self.s.startswith(quote * 3, i):
                return self.end(g, i + 3)
            return i + 1
        if char == "\n" and not frame.triple:
            self.abandon(g, i)
            return i
        return i + 1

    def escape(self, i: int, raw: bool) -> int:
        j = i + 1
        if j < self.n and self.s[j] in "{}":
            return j
        if self.s.startswith("\r\n", j):
            return j + 2
        if not raw and j < self.n:
            end = self.escape_end(i, False)
            if end < 0:
                self.add(INVALID_ESCAPE, i)
            elif self.s[j] == "N":
                return end
        return min(self.n, j + 1)

    def escape_end(self, i: int, is_bytes: bool) -> int:
        s, n = self.s, self.n
        char = s[i + 1]
        digits = (
            2
            if char == "x"
            else 0
            if is_bytes
            else 4
            if char == "u"
            else 8
            if char == "U"
            else 0
        )
        if digits:
            text = s[i + 2 : i + 2 + digits]
            return (
                i + 2 + digits
                if len(text) == digits
                and all(c in "0123456789abcdefABCDEF" for c in text)
                and int(text, 16) <= 0x10FFFF
                else -1
            )
        if char != "N" or is_bytes:
            return i + 2
        if not s.startswith("{", i + 2):
            return -1
        k = i + 3
        while (
            k < n
            and k < i + 131
            and (s[k] in " -" or s[k].isascii() and s[k].isalnum())
        ):
            k += 1
        return k + 1 if k < n and s[k] == "}" and k > i + 3 else -1

    def expression_start(self, pos: int) -> bool:
        while pos < self.n and self.s[pos] in " \t":
            pos += 1
        if pos >= self.n:
            return True
        if self.s[pos] == ".":
            return pos + 1 < self.n and (
                "0" <= self.s[pos + 1] <= "9" or self.s.startswith("...", pos)
            )
        return self.s[pos] not in "}:!=)],;$?`/%&|^><@"

    def bad_field(self):
        if len(self.frames) < 2:
            return
        frame, parent = self.frames[-1], self.frames[-2]
        if frame.kind != F_FIELD or parent.kind != F_STRING or frame.bad:
            return
        frame.bad = True
        self.add(FSTRING_FIELD, frame.start, parent.quote)

    def end(self, index: int, next_pos: int) -> int:
        frame = self.frames[index]
        del self.stack[frame.base :]
        del self.frames[index:]
        return self.touching(frame.quote, next_pos)

    def abandon(self, index: int, newline: int):
        frame = self.frames[index]
        self.add(UNTERMINATED_STRING, frame.start, newline)
        del self.stack[frame.base :]
        del self.frames[index:]

    def close(self, i: int, char: str, base: int) -> int:
        depth = len(self.stack)
        if depth == base:
            if self.frames and char == "}":
                self.frames.pop()
            else:
                self.add(UNMATCHED, i)
            return i + 1
        if CLOSERS[self.s[self.stack[-1]]] == char:
            self.stack.pop()
            return i + 1
        self.add(MISMATCHED, i, self.stack[-1], self.window())
        floor = max(base, depth - 33)
        j = depth - 2
        while j >= floor and CLOSERS[self.s[self.stack[j]]] != char:
            j -= 1
        if j >= floor:
            del self.stack[j:]
        elif self.frames and char == "}":
            del self.stack[base:]
            self.frames.pop()
        return i + 1

    def line(self, pos: int, continuation: bool):
        line = self.line_of(pos)
        self.line_depth[line] = len(self.stack)
        self.line_cont[line] = continuation
        if continuation:
            return
        first = pos
        width = 0
        while first < self.n and self.s[first] in " \t\f":
            width = (width // 8 + 1) * 8 if self.s[first] == "\t" else width + 1
            first += 1
        if first >= self.n or self.s[first] in "\n\r#":
            return
        if not self.stack:
            self.stmt_indent = width
        elif width <= self.stmt_indent and self.starts_statement(first):
            self.suspects.append((line, self.window()))

    def touching(self, qp: int, next_pos: int) -> int:
        if self.touches(next_pos):
            self.add(TEXT_AFTER_STRING, next_pos, qp)
        return next_pos

    def touches(self, pos: int) -> bool:
        if pos >= self.n:
            return False
        char = self.s[pos]
        if "0" <= char <= "9":
            return True
        if not ident_start(char):
            return False
        end = pos + 1
        while end < self.n and ident_part(self.s[end]):
            end += 1
        if (
            end < self.n
            and end - pos <= 2
            and self.s[end] in "'\""
            and prefix(self.s[pos:end]) >= 0
        ):
            return False
        return not iskeyword(self.s[pos:end])

    def starts_statement(self, pos: int) -> bool:
        s, n = self.s, self.n
        if s[pos] == "@":
            return True
        if not ident_start(s[pos]):
            return False
        end = pos + 1
        while end < n and ident_part(s[end]):
            end += 1
        if end < n and s[end] in "'\"":
            return False
        word = s[pos:end]
        if word in STATEMENTS:
            return True
        if word in {"if", "for", "else"}:
            stop = end
            while stop < n and s[stop] not in "\n#":
                stop += 1
            return s[end:stop].rstrip(" \t\r").endswith(":")
        if word in EXPRESSIONS:
            return False
        while end < n and s[end] in " \t":
            end += 1
        if end >= n:
            return False
        char = s[end]
        if char in "(.[":
            return True
        if char == "=":
            return end + 1 >= n or s[end + 1] != "="
        return end + 1 < n and s[end + 1] == "=" and char in "+-*/%&|^@"
