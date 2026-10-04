"""Propose bounded structural repairs, then validate them without execution.

Ported from clj-parinferish. Copyright (c) 2026 Blockether, MIT License.
The scanner is not a Python parser. Only a validated proposal may reach a hook.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import _scanner as scan

(
    TRIPLE_QUOTE,
    CLOSE_QUOTE,
    ESCAPE_QUOTE,
    EXTEND_TRIPLE,
    SWAP_TRIPLE,
    CLOSE_TRIPLE,
    CLOSE_BRACKETS,
    REPLACE_CLOSER,
    REMOVE_CLOSER,
    REMOVE_BACKSLASH,
    TRIM_CONTINUATION,
    NEWLINE_ESCAPES,
    UNESCAPE_QUOTES,
    STRAIGHT_QUOTES,
    DOUBLE_BRACE,
    REMOVE_BRACE,
    REMOVE_MARKER,
    SPLIT_STATEMENT,
    RESTORE_NEWLINE,
    LITERAL_BRACES,
    DOUBLE_BACKSLASH,
    ESCAPE_QUOTES,
) = range(22)
SUSPECT = 100
MAX_FIXES = 32
MAX_PROBLEMS = 8
FIX_KINDS = (
    "triple-quote",
    "close-quote",
    "escape-quote",
    "extend-triple-quote",
    "swap-triple-quote",
    "close-triple-quote",
    "close-brackets",
    "replace-closer",
    "remove-closer",
    "remove-backslash",
    "trim-continuation",
    "newline-escapes",
    "unescape-quotes",
    "straight-quotes",
    "double-brace",
    "remove-brace",
    "remove-marker",
    "split-statement",
    "restore-newline",
    "literal-braces",
    "double-backslash",
    "escape-quotes",
)
PROBLEM_KINDS = (
    "unterminated-string",
    "unterminated-triple-string",
    "unclosed-bracket",
    "unmatched-closer",
    "mismatched-closer",
    "stray-backslash",
    "typographic-quote",
    "single-brace",
    "semicolon-in-brackets",
    "quote-marker",
    "compound-after-semicolon",
    "lost-newline",
    "literal-brace",
    "invalid-escape",
    "text-after-string",
)


@dataclass(frozen=True)
class Diagnostic:
    kind: str
    line: int
    column: int
    message: str


@dataclass(frozen=True)
class RepairResult:
    source: str
    changed: bool
    clean: bool
    fixes: tuple[Diagnostic, ...]
    problems: tuple[Diagnostic, ...]
    spans: tuple[tuple[int, int], ...] = ()

    @property
    def notes(self) -> tuple[str, ...]:
        return tuple(fix.message for fix in self.fixes)


@dataclass
class Candidate:
    kind: int
    ref: int
    ref2: int = -1
    edits: list[tuple[int, int, str]] = field(default_factory=list)
    then: Candidate | None = None
    own: int = 0
    scanner: scan.Scanner | None = None

    def edit(self, at: int, delete: int, insert: str):
        self.edits.append((at, delete, insert))
        return self

    def ordered(self):
        return sorted(self.edits, key=lambda edit: edit[:2])

    def apply(self, source: str) -> str:
        pieces = []
        previous = 0
        for at, delete, insert in self.ordered():
            pieces.extend((source[previous:at], insert))
            previous = at + delete
        pieces.append(source[previous:])
        return "".join(pieces)

    def back(self, pos: int) -> int:
        shift = 0
        for at, delete, insert in self.ordered():
            start = at + shift
            if pos < start:
                break
            if pos < start + len(insert):
                return at
            shift += len(insert) - delete
        return pos - shift


class Triple:
    """Find a triple delimiter while each candidate end only moves forward."""

    def __init__(self, source: str, start: int, quote: str):
        self.s = source
        self.start = start
        self.quotes = (quote, '"' if quote == "'" else "'")
        self.next = [start, start]
        self.runs = [0, 0]
        self.tripled = [False, False]

    def quote(self, end: int) -> str:
        for index, quote in enumerate(self.quotes):
            if end > self.start and self.s[end - 1] == quote:
                continue
            pos, run = self.next[index], self.runs[index]
            while not self.tripled[index] and pos < end:
                char = self.s[pos]
                if char == "\\":
                    pos += 2
                    run = 0
                else:
                    run = run + 1 if char == quote else 0
                    self.tripled[index] = run >= 3
                    pos += 1
            self.next[index], self.runs[index] = pos, run
            if not self.tripled[index]:
                return quote
        return ""


class Work:
    def __init__(self, first: scan.Scanner, error_line: int):
        self.first = first
        self.sc = first
        self.error_line = error_line
        self.budget = 64 * max(first.n, 4096)
        self.fixes: list[Diagnostic] = []
        self.log: list[tuple[int, int, int]] = []
        self.spans: list[tuple[int, int]] = []
        self.problem = scan.Problem(SUSPECT, 0)
        self.problem_line = -1
        self.open_lines: dict[str, set[int]] | None = None

    @property
    def s(self):
        return self.sc.s

    @property
    def n(self):
        return self.sc.n

    def rescan(self, candidate: Candidate, scanner: scan.Scanner):
        source = candidate.apply(scanner.s)
        self.budget -= len(source)
        return scan.Scanner(source).scan()

    def run(self):
        score = self.score(self.sc)
        skipped = set()
        fixed = 0
        while fixed < MAX_FIXES and self.budget > 0 and self.pick(skipped):
            self.budget -= len(self.sc.problems)
            best = None
            best_score = (score[0], score[1], -1)
            for candidate in self.candidates():
                if self.budget <= 0:
                    break
                following = self.rescan(candidate, self.sc)
                if candidate.kind == TRIPLE_QUOTE:
                    next_score = self.chained(candidate, following, 0)
                elif candidate.kind == EXTEND_TRIPLE:
                    next_score = self.extended(candidate, following, 0)
                else:
                    next_score = self.score(following)
                if next_score < best_score:
                    best, best_score = candidate, next_score
                    candidate.scanner = following
            if best is None:
                skipped.add(self.key(self.problem.kind, self.problem.pos))
                continue
            candidate = best
            while candidate is not None:
                self.accept(candidate)
                candidate = candidate.then
            score = best_score
            skipped.clear()
            fixed += 1

    def chained(self, candidate: Candidate, scanner: scan.Scanner, depth: int):
        score = self.score(scanner)
        best = (score[0], score[1], candidate.own)
        end = candidate.edits[1][0] + 5
        problem = self.first_reported(scanner)
        if (
            depth >= 4
            or self.budget <= 0
            or problem is None
            or problem.kind != scan.UNTERMINATED_STRING
            or problem.pos < end
            or scanner.line_of(problem.pos) != scanner.line_of(end - 1)
        ):
            return best
        tried = 0
        for following in self.follow(scanner, problem):
            if following.kind != TRIPLE_QUOTE:
                continue
            if tried == 3 or self.budget <= 0:
                break
            tried += 1
            next_scanner = self.rescan(following, scanner)
            score = self.chained(following, next_scanner, depth + 1)
            score = (score[0], score[1], score[2] + candidate.own)
            if score < best:
                best = score
                candidate.then = following
                following.scanner = next_scanner
        return best

    def extended(self, candidate: Candidate, scanner: scan.Scanner, depth: int):
        best = self.score(scanner)
        problem = self.first_reported(scanner)
        if (
            depth >= 4
            or self.budget <= 0
            or problem is None
            or problem.kind != scan.UNTERMINATED_TRIPLE
        ):
            return best
        tried = 0
        for following in self.follow(scanner, problem):
            if following.kind != EXTEND_TRIPLE:
                continue
            if tried == 4 or self.budget <= 0:
                break
            tried += 1
            next_scanner = self.rescan(following, scanner)
            score = self.extended(following, next_scanner, depth + 1)
            if score < best:
                best = score
                candidate.then = following
                following.scanner = next_scanner
        return best

    def follow(self, scanner: scan.Scanner, problem: scan.Problem):
        previous = self.sc, self.problem
        self.sc, self.problem = scanner, problem
        candidates = []
        if problem.kind == scan.UNTERMINATED_TRIPLE:
            self.unterminated_triple(candidates)
        else:
            self.unterminated(candidates)
        self.sc, self.problem = previous
        return candidates

    @staticmethod
    def reported(scanner: scan.Scanner, problem: scan.Problem):
        return scanner.n + problem.pos if problem.kind == scan.UNCLOSED else problem.pos

    @classmethod
    def first_reported(cls, scanner: scan.Scanner):
        return min(
            scanner.problems,
            key=lambda problem: cls.reported(scanner, problem),
            default=None,
        )

    @staticmethod
    def score(scanner: scan.Scanner):
        hard = sum(
            1 + scanner.lines - scanner.line_of(problem.pos)
            if problem.kind == scan.UNTERMINATED_TRIPLE
            else 1
            for problem in scanner.problems
        )
        return hard, len(scanner.suspects), 0

    def key(self, kind: int, pos: int):
        return self.to_original(pos), kind

    def pick(self, skipped: set):
        candidates = (
            problem
            for problem in self.sc.problems
            if self.key(problem.kind, problem.pos) not in skipped
        )
        best = min(
            candidates,
            key=lambda problem: self.reported(self.sc, problem),
            default=None,
        )
        if best is not None:
            self.problem, self.problem_line = best, -1
            return True
        if self.first.problems:
            return False
        for line, opened in self.sc.suspects:
            pos = self.sc.line_start[line]
            if self.key(SUSPECT, pos) not in skipped and near(
                self.sc, line, opened, self.error_line
            ):
                self.problem = scan.Problem(SUSPECT, pos, opened=opened)
                self.problem_line = line
                return True
        return False

    def candidates(self):
        out = []
        kind, pos = self.problem.kind, self.problem.pos
        if kind != SUSPECT:
            self.short_triple(out)
        if kind == scan.UNTERMINATED_STRING:
            self.unterminated(out)
        elif kind == scan.UNTERMINATED_TRIPLE:
            self.unterminated_triple(out)
        elif kind == scan.UNCLOSED:
            self.unclosed(out)
        elif kind == scan.UNMATCHED:
            out.append(Candidate(REMOVE_CLOSER, pos).edit(pos, 1, ""))
        elif kind == scan.MISMATCHED:
            self.mismatched(out)
        elif kind == scan.STRAY_BACKSLASH:
            self.backslash(out)
        elif kind == scan.TYPOGRAPHIC_QUOTE:
            self.typographic(out)
        elif kind == scan.SEMICOLON:
            self.semicolon(out)
        elif kind == scan.MARKER:
            start = end = pos
            while end < self.n and self.s[end] in ">\u00ab\u00bb":
                end += 1
            if end < self.n and self.s[end] == " ":
                end += 1
            elif start > 0 and self.s[start - 1] == " ":
                start -= 1
            out.append(Candidate(REMOVE_MARKER, pos).edit(start, end - start, ""))
        elif kind == scan.COMPOUND_AFTER_SEMICOLON:
            end = pos + 1
            while end < self.n and self.s[end] in " \t":
                end += 1
            out.append(
                Candidate(SPLIT_STATEMENT, pos).edit(
                    pos, end - pos, "\n" + self.indent_of(pos)
                )
            )
        elif kind == scan.LOST_NEWLINE:
            out.append(
                Candidate(RESTORE_NEWLINE, pos).edit(pos, 1, "\n" + self.indent_of(pos))
            )
        elif kind == scan.FSTRING_BRACE:
            out.append(Candidate(DOUBLE_BRACE, pos).edit(pos, 0, "}"))
            out.append(Candidate(REMOVE_BRACE, pos).edit(pos, 1, ""))
        elif kind == scan.FSTRING_FIELD:
            self.literal_braces(out)
        elif kind == scan.INVALID_ESCAPE:
            out.append(Candidate(DOUBLE_BACKSLASH, pos).edit(pos, 0, "\\"))
        elif kind == scan.TEXT_AFTER_STRING:
            self.quoted_text(out)
        else:
            candidate = self.move_close(self.problem_line, self.problem.opened)
            if candidate is not None:
                out.append(candidate)
        return out

    def quoted_text(self, out: list):
        pos, at = self.problem.pos, self.problem.at
        if self.s[pos] == "n" and any(
            p.kind == scan.LOST_NEWLINE and p.pos == pos for p in self.sc.problems
        ):
            out.append(
                Candidate(RESTORE_NEWLINE, pos).edit(pos, 1, "\n" + self.indent_of(pos))
            )
        quote = self.s[at]
        triple = (
            pos - at >= 6
            and self.s.startswith(quote * 3, at)
            and self.s[pos - 3 : pos - 1] == quote * 2
        )
        width = 3 if triple else 1
        limit = self.n if triple else self.sc.line_end(self.sc.line_of(pos))
        start = pos - width
        pairs = not self.open_on_line(pos, quote)
        candidate = Candidate(ESCAPE_QUOTES, pos, start)
        while pairs and len(candidate.edits) < 32:
            middle = self.delimiter(start + width, limit, quote, width)
            end = (
                -1
                if middle < 0
                else self.delimiter(middle + width, limit, quote, width)
            )
            self.budget -= (end if end >= 0 else limit) - start
            if end < 0:
                break
            candidate.edit(start, 0, "\\").edit(middle, 0, "\\")
            if not self.sc.touches(end + width):
                out.append(candidate)
                break
            start = end
        if (
            width == 1
            and pos > 1
            and self.s[pos - 2].isalpha()
            and self.s[pos].isalpha()
        ):
            out.append(Candidate(ESCAPE_QUOTE, pos, pos - 1).edit(pos - 1, 0, "\\"))

    def open_on_line(self, pos: int, quote: str):
        if self.open_lines is None:
            self.open_lines = {"'": set(), '"': set()}
            for problem in self.first.problems:
                if problem.kind != scan.UNTERMINATED_STRING:
                    continue
                start = problem.pos
                while self.first.s[start] not in "'\"":
                    start += 1
                self.open_lines[self.first.s[start]].add(
                    self.first.line_of(problem.pos)
                )
        return self.first.line_of(self.to_original(pos)) in self.open_lines[quote]

    def delimiter(self, start: int, limit: int, quote: str, width: int):
        pos = start
        while pos < limit:
            if self.s[pos] == "\\":
                pos += 2
                continue
            if self.s[pos] == quote and (
                width == 1 or pos + 2 < limit and self.s.startswith(quote * 3, pos)
            ):
                return pos
            pos += 1
        return -1

    def literal_braces(self, out: list):
        start, at = self.problem.pos, self.problem.at
        quote = self.s[at]
        triple = self.s.startswith(quote * 3, at)
        end = -1
        pos, depth = start + 1, 0
        while pos < self.n:
            char = self.s[pos]
            if char == "\\":
                if pos + 1 < self.n and self.s[pos + 1] not in "{}":
                    pos += 1
            elif (
                char == quote
                and (not triple or self.s.startswith(quote * 3, pos))
                or char == "\n"
                and not triple
            ):
                break
            elif char == "{":
                depth += 1
            elif char == "}":
                if depth == 0:
                    end = pos
                    break
                depth -= 1
            pos += 1
        candidate = Candidate(LITERAL_BRACES, start).edit(start, 0, "{")
        out.append(candidate if end < 0 else candidate.edit(end, 0, "}"))
        if end < 0:
            return
        all_braces = Candidate(LITERAL_BRACES, start)
        pos = start
        while pos <= end:
            if self.s[pos] in "{}":
                all_braces.edit(pos, 0, self.s[pos])
            elif self.s[pos] == "\\" and self.s[pos + 1] not in "{}":
                pos += 1
            pos += 1
        if len(all_braces.edits) > 2:
            out.append(all_braces)

    def earlier_quote(self, out: list, qp: int):
        line = self.sc.line_of(qp)
        if self.sc.line_depth[line] < 0:
            return
        a1 = e1 = a2 = e2 = -1
        ends = []
        pos = self.sc.line_start[line]
        while pos < qp:
            char = self.s[pos]
            if char == "#":
                return
            if char in "'\"":
                end = self.skip_string(pos)
                if end > qp or end - pos < 2 or self.s[end - 1] != char:
                    return
                if len(ends) < 2:
                    self.ends_in_code(ends, pos, end)
                a2, e2, a1, e1 = a1, e1, pos, end
                pos = end - 1
            pos += 1
        made = self.swallowed(out, a1, e1, 0)
        self.swallowed(out, a2, e2, made)
        out.extend(ends)

    def swallowed(self, out: list, start: int, end: int, made: int):
        if start < 0 or self.s.startswith(self.s[start] * 3, start):
            return made
        for pos in range(start + 2, end - 1):
            if made >= 4:
                break
            if self.s[pos] in ")]}" and self.s[pos - 1] not in " \t":
                out.append(Candidate(CLOSE_QUOTE, start).edit(pos, 0, self.s[start]))
                made += 1
        return made

    def ends_in_code(self, out: list, start: int, end: int):
        if self.s.startswith(self.s[start] * 3, start):
            return
        pos, code = end - 1, False
        while pos > start + 1 and self.s[pos - 1] in ",([{)]}: \t":
            code |= self.s[pos - 1] in ",([{"
            pos -= 1
        if code and pos > start + 1 and self.s[pos - 1] != "\\":
            out.append(Candidate(CLOSE_QUOTE, start).edit(pos, 0, self.s[start]))

    def short_triple(self, out: list):
        self.budget -= self.problem.pos
        pos, made = 0, 0
        while pos < self.problem.pos and made < 8:
            char = self.s[pos]
            if char == "\\":
                pos += 1
            elif char == "#":
                while pos + 1 < self.n and self.s[pos + 1] != "\n":
                    pos += 1
            elif char in "'\"":
                end = self.skip_string(pos)
                if (
                    end - pos >= 6
                    and end <= self.problem.pos
                    and self.s.startswith(char * 3, pos)
                    and self.s[end - 3 : end] == char * 3
                    and self.opens_value(pos + 3, end - 3)
                ):
                    k = pos + 3
                    while k < end - 3 and made < 8:
                        if self.s[k] == "\\":
                            k += 1
                        elif self.s[k] == char:
                            run = 1
                            while k + run < end - 3 and self.s[k + run] == char:
                                run += 1
                            if run < 3 and self.s[k + run] in ")]}":
                                out.append(
                                    Candidate(EXTEND_TRIPLE, pos, k).edit(
                                        k + run, 0, char * (3 - run)
                                    )
                                )
                                made += 1
                            k += run - 1
                        k += 1
                pos = end - 1
            pos += 1

    def opens_value(self, start: int, end: int):
        while end > start and self.s[end - 1] in " \t":
            end -= 1
        return end > start and self.s[end - 1] in ":=,([{"

    def unterminated(self, out: list):
        pos, at = self.problem.pos, self.problem.at
        qp = self.quote_at(pos)
        quote = self.s[qp]
        line_start = self.sc.line_start[self.sc.line_of(qp)]
        end, made = qp, 0
        while end >= line_start and made < 2:
            if self.s[end] == quote:
                start = end
                while start > line_start and self.s[start - 1] == quote:
                    start -= 1
                if end - start == 3 and (start == 0 or self.s[start - 1] != "\\"):
                    out.append(Candidate(ESCAPE_QUOTE, pos, start).edit(start, 0, "\\"))
                    made += 1
                end = start
            end -= 1
        end = self.trim_end(qp + 1, at)
        out.append(Candidate(CLOSE_QUOTE, pos).edit(end, 0, quote))
        k = end
        for _ in range(4):
            if k <= qp + 1 or self.s[k - 1] not in ")]},;:":
                break
            k -= 1
            while k > qp + 1 and self.s[k - 1] in " \t":
                k -= 1
            out.append(Candidate(CLOSE_QUOTE, pos).edit(k, 0, quote))
        self.earlier_quote(out, qp)
        made = 0
        for end in range(qp - 1, line_start, -1):
            if made >= 3:
                break
            if (
                self.s[end] == quote
                and self.s[end + 1].isalpha()
                and self.s[end - 1].isalpha()
            ):
                out.append(Candidate(ESCAPE_QUOTE, pos, end).edit(end, 0, "\\"))
                made += 1
        triple = None
        end, found = at, 0
        while end < self.n and found < 8:
            char = self.s[end]
            if char == "\\":
                end += 2
                continue
            if char != quote:
                end += 1
                continue
            run = 1
            while end + run < self.n and self.s[end + run] == quote:
                run += 1
            if run == 1 and self.plausible_end(end + 1):
                if triple is None:
                    triple = Triple(self.s, qp + 1, quote)
                delimiter = triple.quote(end)
                if delimiter:
                    candidate = (
                        Candidate(TRIPLE_QUOTE, pos, end)
                        .edit(qp, 1, delimiter * 3)
                        .edit(end, 1, delimiter * 3)
                    )
                    candidate.own = end - qp - 1
                    out.append(candidate)
                    found += 1
            end += run

    def unterminated_triple(self, out: list):
        pos = self.problem.pos
        qp = self.quote_at(pos)
        quote = self.s[qp]
        other = '"' if quote == "'" else "'"
        ends = []
        end = qp + 3
        while end < self.n:
            char = self.s[end]
            if char == "\\":
                end += 1
            elif char == quote:
                run = 1
                while end + run < self.n and self.s[end + run] == quote:
                    run += 1
                if run < 3 and self.plausible_end(end + run):
                    ends.append(
                        Candidate(EXTEND_TRIPLE, pos, end).edit(
                            end + run, 0, quote * (3 - run)
                        )
                    )
                end += run - 1
            elif char == other and self.s.startswith(other * 3, end):
                if self.plausible_end(end + 3):
                    ends.append(
                        Candidate(SWAP_TRIPLE, pos, end).edit(end, 3, quote * 3)
                    )
                end += 2
            end += 1
        out.extend(
            candidate
            for index, candidate in enumerate(ends)
            if index < 4 or index >= len(ends) - 4
        )
        end = self.trim_end(qp + 3, self.n)
        out.append(Candidate(CLOSE_TRIPLE, pos).edit(end, 0, quote * 3))
        k = end
        for _ in range(4):
            if k <= qp + 3 or self.s[k - 1] not in ")]},;:":
                break
            k -= 1
            while k > qp + 3 and self.s[k - 1] in " \t":
                k -= 1
            out.append(Candidate(CLOSE_TRIPLE, pos).edit(k, 0, quote * 3))

    def unclosed(self, out: list):
        opened = tuple(
            problem.pos for problem in self.sc.problems if problem.kind == scan.UNCLOSED
        )
        made = 0
        for line, brackets in self.sc.suspects:
            if made >= 3:
                break
            if self.problem.pos in brackets:
                candidate = self.move_close(line, brackets)
                if candidate is not None:
                    out.append(candidate)
                    made += 1
        self.sibling(out, opened[-1], self.n)
        insertion = self.insertion_before(self.sc.lines)
        if insertion > opened[-1]:
            out.append(
                Candidate(CLOSE_BRACKETS, opened[0]).edit(
                    insertion, 0, self.closers(opened)
                )
            )
        line = self.sc.line_of(opened[-1])
        start = len(opened) - 1
        while start > 0 and self.sc.line_of(opened[start - 1]) == line:
            start -= 1
        earlier = self.insertion_before(line + 1)
        if earlier > opened[-1] and earlier != insertion:
            out.append(
                Candidate(CLOSE_BRACKETS, opened[start]).edit(
                    earlier, 0, self.closers(opened, start)
                )
            )

    def semicolon(self, out: list):
        opened, pos = self.problem.opened, self.problem.pos
        made = 0
        for line, brackets in self.sc.suspects:
            if made >= 2:
                break
            if self.sc.line_start[line] < pos and opened[-1] in brackets:
                candidate = self.move_close(line, brackets)
                if candidate is not None:
                    out.append(candidate)
                    made += 1
        self.sibling(out, opened[-1], pos)
        insertion = self.trim_end(opened[-1] + 1, pos)
        out.append(
            Candidate(CLOSE_BRACKETS, opened[0]).edit(
                insertion, 0, self.closers(opened)
            )
        )

    def mismatched(self, out: list):
        pos, at, opened = self.problem.pos, self.problem.at, self.problem.opened
        char = self.s[pos]
        j = len(opened) - 2
        while j >= 0 and scan.CLOSERS[self.s[opened[j]]] != char:
            j -= 1
        self.string_closer(out, char)
        if j >= 0 and char == "}":
            self.key_after_comma(out, opened, j + 1)
        self.sibling(out, at, pos)
        if j >= 0:
            out.append(
                Candidate(CLOSE_BRACKETS, opened[j + 1]).edit(
                    pos, 0, self.closers(opened, j + 1)
                )
            )
        out.append(
            Candidate(REPLACE_CLOSER, pos, at).edit(pos, 1, scan.CLOSERS[self.s[at]])
        )
        out.append(Candidate(REMOVE_CLOSER, pos).edit(pos, 1, ""))
        start_line, end_line = self.sc.line_of(at), self.sc.line_of(pos)
        made = 0
        for line, brackets in self.sc.suspects:
            if made >= 2:
                break
            if start_line < line <= end_line and at in brackets:
                candidate = self.move_close(line, brackets)
                if candidate is not None:
                    out.append(candidate)
                    made += 1

    def string_closer(self, out: list, closer: str):
        opener = {value: key for key, value in scan.CLOSERS.items()}[closer]
        depth = 0
        pos = self.problem.at + 1
        limit = self.problem.pos
        while pos < limit:
            char = self.s[pos]
            if char in "'\"":
                end = self.skip_string(pos)
                after = end
                while after < limit and self.s[after] in " \t":
                    after += 1
                if (
                    depth == 1
                    and after < limit
                    and self.s[after] == closer
                    and end - 1 > pos
                    and self.s[end - 1] == char
                ):
                    content = self.s[pos + 1 : end - 1]
                    if content.count(opener) > content.count(closer):
                        out.append(Candidate(REMOVE_CLOSER, after).edit(after, 1, ""))
                        return
                pos = end - 1
            elif char == "#":
                while pos + 1 < limit and self.s[pos + 1] != "\n":
                    pos += 1
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth < 0:
                    return
            pos += 1

    def key_after_comma(self, out: list, opened: tuple, start: int):
        level = nest = 0
        comma = -1
        pos = opened[start] + 1
        limit = self.problem.pos
        while pos < limit:
            char = self.s[pos]
            if char in "'\"":
                end = self.skip_string(pos)
                if comma >= 0:
                    after = end
                    while after < limit and self.s[after] in " \t":
                        after += 1
                    if (
                        after + 1 < limit
                        and self.s[after] == ":"
                        and self.s[after + 1] != "="
                        and self.s[opened[start + level]] != "{"
                    ):
                        insertion = self.trim_end(opened[start] + 1, comma)
                        closers = self.closers(opened[start : start + level + 1])
                        out.append(
                            Candidate(CLOSE_BRACKETS, opened[start]).edit(
                                insertion, 0, closers
                            )
                        )
                        return
                comma = -1
                pos = end - 1
            elif char == "#":
                while pos + 1 < limit and self.s[pos + 1] != "\n":
                    pos += 1
            elif char in "([{":
                comma = -1
                if (
                    nest == 0
                    and start + level + 1 < len(opened)
                    and opened[start + level + 1] == pos
                ):
                    level += 1
                else:
                    nest += 1
            elif char in ")]}":
                comma = -1
                nest -= 1
                if nest < 0:
                    return
            elif char == ",":
                comma = pos if nest == 0 else -1
            elif char not in " \t\n\r":
                comma = -1
            pos += 1

    def sibling(self, out: list, opener: int, limit: int):
        if self.s[opener] != "(":
            return
        start = opener
        while start > 0 and (
            scan.ident_part(self.s[start - 1]) or self.s[start - 1] == "."
        ):
            start -= 1
        if start == opener or not scan.ident_start(self.s[start]):
            return
        depth = 0
        pos = opener + 1
        while pos < limit:
            char = self.s[pos]
            if char in "'\"":
                pos = self.skip_string(pos) - 1
            elif char == "#":
                while pos + 1 < limit and self.s[pos + 1] != "\n":
                    pos += 1
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth < 0:
                    return
            elif char == "," and depth == 0:
                after = pos + 1
                while after < limit and self.s[after] in " \t\n":
                    after += 1
                k = 0
                while (
                    start + k < opener
                    and after + k < limit
                    and self.s[after + k] == self.s[start + k]
                ):
                    k += 1
                if (
                    start + k == opener
                    and after + k < limit
                    and self.s[after + k] == "("
                ):
                    out.append(Candidate(CLOSE_BRACKETS, opener).edit(pos, 0, ")"))
                    return
            pos += 1

    def skip_string(self, pos: int):
        quote = self.s[pos]
        triple = self.s.startswith(quote * 3, pos)
        end = pos + (3 if triple else 1)
        while end < self.n:
            char = self.s[end]
            if char == "\\":
                end += 1
            elif char == "\n" and not triple:
                return end
            elif char == quote and (not triple or self.s.startswith(quote * 3, end)):
                return end + (3 if triple else 1)
            end += 1
        return self.n

    def backslash(self, out: list):
        pos = self.problem.pos
        line = self.sc.line_of(pos)
        line_end = self.sc.line_end(line)
        char = self.s[pos + 1] if pos + 1 < self.n else "\n"
        if char in "'\"":
            candidate = Candidate(UNESCAPE_QUOTES, pos)
            k = pos
            while k + 1 < line_end:
                if self.s[k] == "\\":
                    if self.s[k + 1] in "'\"":
                        candidate.edit(k, 1, "")
                    k += 1
                k += 1
            out.append(candidate)
        if char == "n":
            candidate = Candidate(NEWLINE_ESCAPES, pos)
            for problem in self.sc.problems:
                p = problem.pos
                if (
                    problem.kind == scan.STRAY_BACKSLASH
                    and p + 1 < self.n
                    and self.s[p + 1] == "n"
                    and self.sc.line_of(p) == line
                ):
                    candidate.edit(p, 2, "\n")
            out.append(candidate)
        k = pos + 1
        while k < self.n and self.s[k] in " \t":
            k += 1
        if k > pos + 1 and (k >= self.n or self.s[k] in "\n\r"):
            out.append(Candidate(TRIM_CONTINUATION, pos).edit(pos + 1, k - pos - 1, ""))
        out.append(Candidate(REMOVE_BACKSLASH, pos).edit(pos, 1, ""))

    def typographic(self, out: list):
        pos = self.problem.pos
        quotes = "\u2018\u2019" if self.s[pos] in "\u2018\u2019" else "\u201c\u201d"
        straight = "'" if quotes == "\u2018\u2019" else '"'
        for k in range(pos + 1, self.sc.line_end(self.sc.line_of(pos))):
            if self.s[k] in quotes:
                out.append(
                    Candidate(STRAIGHT_QUOTES, pos)
                    .edit(pos, 1, straight)
                    .edit(k, 1, straight)
                )
                break
        out.append(Candidate(STRAIGHT_QUOTES, pos).edit(pos, 1, straight))

    def move_close(self, line: int, opened: tuple):
        insertion = self.insertion_before(line)
        if insertion < 0 or insertion <= opened[-1]:
            return None
        closers = self.closers(opened)
        candidate = Candidate(CLOSE_BRACKETS, opened[0]).edit(insertion, 0, closers)
        for _ in opened:
            if self.budget <= 0:
                break
            scanner = self.rescan(candidate, self.sc)
            start = insertion + len(closers)
            problem = min(
                (p for p in scanner.problems if p.pos >= start),
                key=lambda p: p.pos,
                default=None,
            )
            if problem is None or problem.kind != scan.UNMATCHED:
                break
            candidate.edit(candidate.back(problem.pos), 1, "")
        return candidate

    def insertion_before(self, line: int):
        for previous in range(line - 1, -1, -1):
            ends_in_code = (
                self.sc.line_depth[previous + 1] >= 0
                if previous + 1 < self.sc.lines
                else self.sc.ends_in_code
            )
            if not ends_in_code:
                return -1
            start = self.sc.line_start[previous]
            comment = self.sc.line_comment[previous]
            end = comment if comment >= 0 else self.sc.line_end(previous)
            end = self.trim_end(start, end)
            if (
                previous + 1 < self.sc.lines
                and self.sc.line_cont[previous + 1]
                and end > start
                and self.s[end - 1] == "\\"
            ):
                end = self.trim_end(start, end - 1)
            if end > start:
                return end
        return -1

    def plausible_end(self, pos: int):
        while pos < self.n and self.s[pos] in " \t":
            pos += 1
        if pos >= self.n:
            return True
        char = self.s[pos]
        if char in ",)]}:;.+%*=#\n\r<>!":
            return True
        if not scan.ident_start(char):
            return False
        end = pos + 1
        while end < self.n and scan.ident_part(self.s[end]):
            end += 1
        return self.s[pos:end] in {"if", "else", "for", "in", "is", "and", "or", "not"}

    def quote_at(self, pos: int):
        while self.s[pos] not in "'\"":
            pos += 1
        return pos

    def indent_of(self, pos: int):
        start = self.sc.line_start[self.sc.line_of(pos)]
        end = start
        while end < pos and self.s[end] in " \t":
            end += 1
        return self.s[start:end]

    def trim_end(self, start: int, end: int):
        while end > start and self.s[end - 1] in " \t\r\f\n":
            end -= 1
        return end

    def closers(self, opened: tuple, start: int = 0):
        return "".join(scan.CLOSERS[self.s[pos]] for pos in reversed(opened[start:]))

    def accept(self, candidate: Candidate):
        self.fixes.append(self.describe(candidate))
        self.spans.extend(
            (self.line(at), self.line(at + max(0, delete - 1)))
            for at, delete, _ in candidate.edits
        )
        self.log.extend(
            (at, delete, len(insert))
            for at, delete, insert in reversed(candidate.ordered())
        )
        assert candidate.scanner is not None
        self.sc = candidate.scanner

    def to_original(self, pos: int):
        for at, delete, inserted in reversed(self.log):
            if pos >= at + inserted:
                pos += delete - inserted
            elif pos > at:
                pos = at
        return pos

    def line(self, pos: int):
        return self.first.line_of(self.to_original(pos)) + 1

    def column(self, pos: int):
        return self.first.column(self.to_original(pos))

    def places(self, candidate: Candidate):
        ordered = candidate.ordered()
        first, last = ordered[0][0], ordered[-1][0]
        one = self.line(first) == self.line(last)
        if len(ordered) > 4:
            if one:
                return f"from column {self.column(first)} to column {self.column(last)}"
            return f"from line {self.line(first)}, column {self.column(first)} to line {self.line(last)}, column {self.column(last)}"
        places = [
            str(self.column(at))
            if one
            else f"line {self.line(at)}, column {self.column(at)}"
            for at, _, _ in ordered
        ]
        prefix = "at columns " if one else "at "
        return prefix + (
            ", ".join(places[:-1]) + " and " + places[-1]
            if len(places) > 1
            else places[0]
        )

    def describe(self, candidate: Candidate):
        kind = candidate.kind
        ref, ref2 = candidate.ref, candidate.ref2
        pos = (
            ref2
            if kind in {ESCAPE_QUOTE, ESCAPE_QUOTES, EXTEND_TRIPLE, SWAP_TRIPLE}
            else candidate.edits[0][0]
        )
        line, column = self.line(pos), self.column(pos)
        insert = candidate.edits[0][2]
        count = len(candidate.edits)
        where = f"line {line}: "
        if kind == TRIPLE_QUOTE:
            message = f"made the string at column {column} triple-quoted ({insert}) because its text continues onto the next lines; it now ends on line {self.line(ref2)}"
        elif kind == CLOSE_QUOTE:
            message = f"added the missing closing {insert} at column {column}"
        elif kind == ESCAPE_QUOTE:
            message = f"escaped the {self.s[ref2]} at column {column} that ended the string early"
        elif kind == EXTEND_TRIPLE:
            message = f"completed the closing {self.s[ref2] * 3} at column {column} of the triple-quoted string from line {self.line(ref)}"
        elif kind == SWAP_TRIPLE:
            message = f"changed the closing {self.s[ref2 : ref2 + 3]} at column {column} to {insert} to match the string from line {self.line(ref)}"
        elif kind == CLOSE_TRIPLE:
            message = f"added the missing closing {insert} at column {column} for the triple-quoted string from line {self.line(ref)}"
        elif kind == CLOSE_BRACKETS:
            what = f"'{self.s[ref]}'" if len(insert) == 1 else "the brackets"
            message = f"added '{insert}' at column {column} to close {what} from line {self.line(ref)}"
            for index, (at, _, _) in enumerate(candidate.edits[1:]):
                message += (
                    (", and removed the extra '" if index == 0 else ", '")
                    + self.s[at]
                    + f"' on line {self.line(at)}"
                )
        elif kind == REPLACE_CLOSER:
            message = f"replaced '{self.s[pos]}' at column {column} with '{insert}' to match '{self.s[ref2]}' from line {self.line(ref2)}"
        elif kind == REMOVE_CLOSER:
            message = f"removed the unmatched '{self.s[pos]}' at column {column}"
        elif kind == REMOVE_BACKSLASH:
            message = f"removed the stray backslash at column {column}"
        elif kind == TRIM_CONTINUATION:
            message = f"removed the spaces after the line-continuation backslash at column {column - 1}"
        elif kind == NEWLINE_ESCAPES:
            message = "turned the literal \\n outside strings into line breaks"
        elif kind == UNESCAPE_QUOTES:
            message = f"removed the backslashes before quotes outside strings, from column {column}"
        elif kind == STRAIGHT_QUOTES:
            message = f"replaced the typographic quotes at column {column} with straight quotes"
        elif kind == DOUBLE_BRACE:
            message = f"doubled the single '}}' at column {column} in the f-string"
        elif kind == REMOVE_BRACE:
            message = f"removed the single '}}' at column {column} in the f-string"
        elif kind == LITERAL_BRACES:
            middle = (
                ""
                if count == 1
                else " and its closing '}'"
                if count == 2
                else ", its closing '}' and the braces between them"
            )
            message = f"doubled the '{{' at column {column}{middle} in the f-string, which held text rather than an expression"
        elif kind == DOUBLE_BACKSLASH:
            message = f"doubled the backslash at column {column} so the string keeps it as text instead of an invalid escape"
        elif kind == ESCAPE_QUOTES:
            amount = f"{count} " if count > 4 else ""
            message = f"escaped the {amount}{self.s[ref2]} {self.places(candidate)} so the string keeps them as text"
        elif kind == REMOVE_MARKER:
            message = (
                f"removed the quote marker {self.s[ref]} at column {self.column(ref)}"
            )
        elif kind == SPLIT_STATEMENT:
            message = (
                f"moved the statement after ';' at column {column} onto its own line"
            )
        else:
            message = f"turned the 'n' at column {column} back into the line break it stood for"
        return Diagnostic(FIX_KINDS[kind], line, column, where + message)


def near(scanner: scan.Scanner, line: int, opened: tuple, error_line: int):
    return error_line <= 0 or scanner.line_of(opened[0]) + 1 <= error_line <= line + 1


def describe_problem(scanner: scan.Scanner, problem: scan.Problem):
    s = scanner.s
    pos, at, kind = problem.pos, problem.at, problem.kind
    line, column = scanner.line_of(pos), scanner.column(pos)
    where = f"line {line + 1}, column {column}: "
    if kind == scan.UNTERMINATED_STRING:
        end = pos
        while s[end] not in "'\"":
            end += 1
        what = "f-string" if any(c.lower() in "ft" for c in s[pos:end]) else "string"
        message = f"the {what} is not closed on its line"
    elif kind == scan.UNTERMINATED_TRIPLE:
        message = "the triple-quoted string is never closed"
    elif kind == scan.UNCLOSED:
        message = f"'{s[pos]}' is never closed"
    elif kind == scan.UNMATCHED:
        message = f"'{s[pos]}' has no opening bracket"
    elif kind == scan.MISMATCHED:
        message = f"'{s[pos]}' does not match '{s[at]}' from line {scanner.line_of(at) + 1}, column {scanner.column(at)}"
    elif kind == scan.STRAY_BACKSLASH:
        message = "backslash outside a string; there it may only end a line"
    elif kind == scan.TYPOGRAPHIC_QUOTE:
        message = f"typographic quote {s[pos]}; Python needs straight quotes (' or \")"
    elif kind == scan.SEMICOLON:
        message = f"';' ends the statement while '{s[at]}' from line {scanner.line_of(at) + 1}, column {scanner.column(at)} is still open"
    elif kind == scan.FSTRING_BRACE:
        message = "single '}' in an f-string; write '}}' for a literal brace"
    elif kind == scan.FSTRING_FIELD:
        message = "'{' opens an f-string field that holds no Python expression; write '{{' and '}}' for literal braces"
    elif kind == scan.INVALID_ESCAPE:
        message = f"'\\{s[pos + 1]}' is not a valid escape; write '\\\\{s[pos + 1]}' to keep the backslash, or use a raw string"
    elif kind == scan.TEXT_AFTER_STRING:
        opener_line = scanner.line_of(at)
        extra = "" if opener_line == line else f"line {opener_line + 1}, "
        message = f"the string from {extra}column {scanner.column(at)} ends right before this text"
    elif kind == scan.MARKER:
        message = f"'{s[pos]}' is a quote marker, not Python; remove it"
    elif kind == scan.COMPOUND_AFTER_SEMICOLON:
        message = "a compound statement cannot follow ';'; start it on its own line"
    else:
        message = (
            f"'n' right after '{s[pos - 1]}' looks like a \\n that lost its backslash"
        )
    return Diagnostic(PROBLEM_KINDS[kind], line + 1, column, where + message)


def diagnose(scanner: scan.Scanner, error_line: int):
    if scanner.problems:
        return tuple(
            describe_problem(scanner, problem)
            for problem in sorted(scanner.problems, key=lambda p: p.pos)[:MAX_PROBLEMS]
        )
    problems = []
    for line, opened in scanner.suspects:
        if not near(scanner, line, opened, error_line):
            continue
        pos = opened[0]
        message = f"line {line + 1} starts a new statement while '{scanner.s[pos]}' from line {scanner.line_of(pos) + 1}, column {scanner.column(pos)} is still open"
        problems.append(Diagnostic("open-bracket-at-statement", line + 1, 1, message))
        if len(problems) == 3:
            break
    return tuple(problems)


def repair(source: str, *, error_line: int = 0) -> RepairResult:
    """Repair delimiters within a fixed work budget, without running the source.

    The result's clean flag describes delimiters, not Python grammar.
    Validate the source before writing a file or executing a repaired block.
    """
    first = scan.Scanner(source).scan()
    problems = diagnose(first, error_line)
    if not problems:
        return RepairResult(source, False, True, (), ())
    work = Work(first, error_line)
    work.run()
    return RepairResult(
        work.s,
        work.s != source,
        not work.sc.problems,
        tuple(work.fixes),
        problems,
        tuple(work.spans),
    )


def repair_source(
    source: str,
    *,
    original: str | None = None,
    spans: Sequence[Sequence[int]] = (),
    parses_clean: Callable[[str], bool],
) -> RepairResult | None:
    """Return a changed, validated proposal, or leave the source unchanged.

    Parsing never executes source. The hook supplies the parser for its context.
    For a patch, every repair must stay in the supplied edited lines.
    """
    if parses_clean(source):
        return None
    result = repair(source)
    if original is not None and any(
        not any(start <= first and last <= end for start, end in spans)
        for first, last in result.spans
    ):
        return None
    if result.changed and result.notes and result.clean and parses_clean(result.source):
        return result
    return None
