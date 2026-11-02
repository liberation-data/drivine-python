"""
The regular expressions the functions of one SQLite connection read, as drivine4j's `SqlitePatterns` reads them: Java's
expressions, compiled with `UNICODE_CHARACTER_CLASS`, with what Postgres's add that Java's do not read, a class written
as POSIX names it, `[:alnum:]`, which is the same class, of letters and digits in any script, as on a Postgres database
whose text is Unicode. So an expression a full-text search writes for Postgres reads the same here (see TextQuery), and
one a Cypher `=~` gives, `text REGEXP ('^(?:' || pattern || ')\\z')`, matches as on drivine4j's SQLite and Neo4j.

Python's `re` reads another dialect, so each expression is written as Python reads it (see JavaPattern):

- a flag group within the expression, `x(?i)abc`, sets its flags to the end of the group it is in, alternatives
  after a `|` among it, as Java reads it: Python reads one only at the start, so it is written as a group of its own
  around the rest, `x(?i:abc)`, closed and opened again around each `|`. `(?x)` and `(?d)` are read here, and the
  expression written without them; `(?u)`, `(?U)` and `(?c)` change nothing Python reads;
- `.`, `^`, `$` and `\\Z` know each of Java's line terminators (`\\r\\n`, `\\r`, `\\u0085`, `\\u2028`, `\\u2029`),
  where Python's know only `\\n`; `\\z` is the end of the text, which Python writes `\\Z`, and only an escape: in
  `a\\\\z` the backslash is escaped, and `z` is itself;
- `\\w`, `\\b`, `\\s`, `\\h`, `\\v`, `\\R`, `\\p{…}` and the POSIX names are Java's Unicode classes, written out as
  ranges: `\\w` is a letter of any script, a mark, a digit, a connector or a joiner, where Python's leaves the marks
  out and takes in every number (`²`, `½`); `\\s` is Unicode's white space, where Python's takes in `\\x1c`–`\\x1f`;
- a class within a class, `[a-z[0-9]]`, and an intersection, `[a-z&&[^aeiou]]`, are worked out as the ranges they
  hold; `\\Q…\\E`, `\\x{…}`, `\\0…`, `\\c…`, `\\e`, `\\k<name>`, `(?<name>…)` and a back reference that reads as many
  digits as there are groups are written as Python writes them.

A property Python keeps no table of, a script (`\\p{IsLatin}`), a block (`\\p{InGreek}`), `\\p{IsIdeographic}` or
`\\p{IsEmoji}`, and `\\G`, `\\X` and `\\b{g}`, are refused, with a message that names them, rather than read otherwise.
Python's tables are of the Unicode its version knows, and Java's of its own, so a character new to one may be classed
otherwise by the other. An empty match next to a character outside the basic plane is found once here, where Java,
which reads UTF-16, finds it again between the two halves of the character (`drivine_regexp_count('😀a', '')` is 3
here, 4 on drivine4j).
"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import cache

from drivine.drivine_error import DrivineError

Ranges = list[tuple[int, int]]
"""A set of code points, as the ranges of it, in order, none touching another."""

MAX_CODE_POINT = 0x10FFFF

_OTHER_ALPHABETIC = (
    "345-345 5B0-5BD 5BF-5BF 5C1-5C2 5C4-5C5 5C7-5C7 610-61A 64B-657 659-65F 670-670 6D6-6DC 6E1-6E4 6E7-6E8 6ED-6ED "
    "711-711 730-73F 7A6-7B0 816-817 81B-823 825-827 829-82C 8D4-8DF 8E3-8E9 8F0-903 93A-93B 93E-94C 94E-94F 955-957 "
    "962-963 981-983 9BE-9C4 9C7-9C8 9CB-9CC 9D7-9D7 9E2-9E3 A01-A03 A3E-A42 A47-A48 A4B-A4C A51-A51 A70-A71 A75-A75 "
    "A81-A83 ABE-AC5 AC7-AC9 ACB-ACC AE2-AE3 AFA-AFC B01-B03 B3E-B44 B47-B48 B4B-B4C B56-B57 B62-B63 B82-B82 BBE-BC2 "
    "BC6-BC8 BCA-BCC BD7-BD7 C00-C04 C3E-C44 C46-C48 C4A-C4C C55-C56 C62-C63 C81-C83 CBE-CC4 CC6-CC8 CCA-CCC CD5-CD6 "
    "CE2-CE3 CF3-CF3 D00-D03 D3E-D44 D46-D48 D4A-D4C D57-D57 D62-D63 D81-D83 DCF-DD4 DD6-DD6 DD8-DDF DF2-DF3 E31-E31 "
    "E34-E3A E4D-E4D EB1-EB1 EB4-EB9 EBB-EBC ECD-ECD F71-F83 F8D-F97 F99-FBC 102B-1036 1038-1038 103B-103E 1056-1059 "
    "105E-1060 1062-1064 1067-106D 1071-1074 1082-108D 108F-108F 109A-109D 1712-1713 1732-1733 1752-1753 1772-1773 "
    "17B6-17C8 1885-1886 18A9-18A9 1920-192B 1930-1938 1A17-1A1B 1A55-1A5E 1A61-1A74 1ABF-1AC0 1ACC-1ACE 1B00-1B04 "
    "1B35-1B43 1B80-1B82 1BA1-1BA9 1BAC-1BAD 1BE7-1BF1 1C24-1C36 1DE7-1DF4 24B6-24E9 2DE0-2DFF A674-A67B A69E-A69F "
    "A802-A802 A80B-A80B A823-A827 A880-A881 A8B4-A8C3 A8C5-A8C5 A8FF-A8FF A926-A92A A947-A952 A980-A983 A9B4-A9BF "
    "A9E5-A9E5 AA29-AA36 AA43-AA43 AA4C-AA4D AA7B-AA7D AAB0-AAB0 AAB2-AAB4 AAB7-AAB8 AABE-AABE AAEB-AAEF AAF5-AAF5 "
    "ABE3-ABEA FB1E-FB1E 10376-1037A 10A01-10A03 10A05-10A06 10A0C-10A0F 10D24-10D27 10EAB-10EAC 11000-11002 "
    "11038-11045 11073-11074 11080-11082 110B0-110B8 110C2-110C2 11100-11102 11127-11132 11145-11146 11180-11182 "
    "111B3-111BF 111CE-111CF 1122C-11234 11237-11237 1123E-1123E 11241-11241 112DF-112E8 11300-11303 1133E-11344 "
    "11347-11348 1134B-1134C 11357-11357 11362-11363 11435-11441 11443-11445 114B0-114C1 115AF-115B5 115B8-115BE "
    "115DC-115DD 11630-1163E 11640-11640 116AB-116B5 1171D-1172A 1182C-11838 11930-11935 11937-11938 1193B-1193C "
    "11940-11940 11942-11942 119D1-119D7 119DA-119DF 119E4-119E4 11A01-11A0A 11A35-11A39 11A3B-11A3E 11A51-11A5B "
    "11A8A-11A97 11C2F-11C36 11C38-11C3E 11C92-11CA7 11CA9-11CB6 11D31-11D36 11D3A-11D3A 11D3C-11D3D 11D3F-11D41 "
    "11D43-11D43 11D47-11D47 11D8A-11D8E 11D90-11D91 11D93-11D96 11EF3-11EF6 11F00-11F01 11F03-11F03 11F34-11F3A "
    "11F3E-11F40 16F4F-16F4F 16F51-16F87 16F8F-16F92 16FF0-16FF1 1BC9E-1BC9E 1E000-1E006 1E008-1E018 1E01B-1E021 "
    "1E023-1E024 1E026-1E02A 1E08F-1E08F 1E947-1E947 1F130-1F149 1F150-1F169 1F170-1F189"
)
"""
The characters Unicode calls alphabetic that are not letters, nor letter numbers: vowel signs and other marks, and the
circled letters. Python keeps no table of the property, so this is Java 21's (Unicode 15), as drivine4j's JDK reads it.
"""

_WHITE_SPACE: Ranges = [
    (0x09, 0x0D), (0x20, 0x20), (0x85, 0x85), (0xA0, 0xA0), (0x1680, 0x1680), (0x2000, 0x200A),
    (0x2028, 0x2029), (0x202F, 0x202F), (0x205F, 0x205F), (0x3000, 0x3000),
]  # fmt: skip
"""Unicode's White_Space, Java's `\\s` with UNICODE_CHARACTER_CLASS."""

_HORIZONTAL: Ranges = [
    (0x09, 0x09), (0x20, 0x20), (0xA0, 0xA0), (0x1680, 0x1680), (0x180E, 0x180E), (0x2000, 0x200A),
    (0x202F, 0x202F), (0x205F, 0x205F), (0x3000, 0x3000),
]  # fmt: skip
"""Java's `\\h`."""

_VERTICAL: Ranges = [(0x0A, 0x0D), (0x85, 0x85), (0x2028, 0x2029)]
"""Java's `\\v`."""

_HEX_DIGIT: Ranges = [(0x30, 0x39), (0x41, 0x46), (0x61, 0x66), (0xFF10, 0xFF19), (0xFF21, 0xFF26), (0xFF41, 0xFF46)]

_LINE_TERMINATORS = "\\n\\r\\x85\\u2028\\u2029"
"""Java's line terminators, but for `\\r\\n`, which is two of them read as one."""

_POSIX = {
    "[:alnum:]": "\\p{Alnum}",
    "[:alpha:]": "\\p{Alpha}",
    "[:digit:]": "\\p{Digit}",
    "[:space:]": "\\p{Space}",
    "[:upper:]": "\\p{Upper}",
    "[:lower:]": "\\p{Lower}",
    "[:punct:]": "\\p{Punct}",
}
"""The POSIX classes a bracket expression may name, as Java names them: drivine4j's."""


def union(*sets: Ranges) -> Ranges:
    merged: Ranges = []
    for low, high in sorted(it for ranges in sets for it in ranges):
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def complement(ranges: Ranges) -> Ranges:
    gaps: Ranges = []
    at = 0
    for low, high in ranges:
        if low > at:
            gaps.append((at, low - 1))
        at = high + 1
    if at <= MAX_CODE_POINT:
        gaps.append((at, MAX_CODE_POINT))
    return gaps


def intersection(a: Ranges, b: Ranges) -> Ranges:
    return complement(union(complement(a), complement(b)))


def _of(predicate_ranges: list[int]) -> Ranges:
    """The ranges of the code points, given in order."""
    ranges: Ranges = []
    for cp in predicate_ranges:
        if ranges and cp == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], cp)
        else:
            ranges.append((cp, cp))
    return ranges


@cache
def _categories() -> dict[str, Ranges]:
    """Each general category's characters, as Python's Unicode tables class them."""
    found: dict[str, Ranges] = {}
    start, current = 0, unicodedata.category("\0")
    for cp in range(1, MAX_CODE_POINT + 2):
        category = unicodedata.category(chr(cp)) if cp <= MAX_CODE_POINT else ""
        if category != current:
            found.setdefault(current, []).append((start, cp - 1))
            start, current = cp, category
    return found


def _category(*names: str) -> Ranges:
    categories = _categories()
    return union(*(ranges for name, ranges in categories.items() if any(name.startswith(it) for it in names)))


@cache
def _cased(lower: bool) -> Ranges:
    """Unicode's Lowercase or Uppercase, which Python's `islower` and `isupper` read of a character."""
    test = str.islower if lower else str.isupper
    return _of([cp for cp in range(MAX_CODE_POINT + 1) if test(chr(cp))])


@cache
def _alphabetic() -> Ranges:
    other = [(int(low, 16), int(high, 16)) for low, high in (it.split("-") for it in _OTHER_ALPHABETIC.split())]
    return union(_category("L", "Nl"), other)


@cache
def _word() -> Ranges:
    """Java's `\\w` with UNICODE_CHARACTER_CLASS: alphabetic, a mark, a decimal digit, a connector or a joiner."""
    return union(_alphabetic(), _category("Mn", "Me", "Mc", "Nd", "Pc"), [(0x200C, 0x200D)])


def _blank() -> Ranges:
    return intersection(_WHITE_SPACE, complement(union(_category("Zl", "Zp"), [(0x0A, 0x0D), (0x85, 0x85)])))


def _graph() -> Ranges:
    return complement(union(_WHITE_SPACE, _category("Cc", "Cs", "Cn")))


_NONCHARACTERS: Ranges = [(0xFDD0, 0xFDEF)] + [(p * 0x10000 + 0xFFFE, p * 0x10000 + 0xFFFF) for p in range(17)]

_NAMED = {
    # The POSIX names, as Java reads them with UNICODE_CHARACTER_CLASS.
    "ALPHA": _alphabetic,
    "LOWER": lambda: _cased(True),
    "UPPER": lambda: _cased(False),
    "SPACE": lambda: _WHITE_SPACE,
    "PUNCT": lambda: _category("P"),
    "XDIGIT": lambda: union(_category("Nd"), _HEX_DIGIT),
    "ALNUM": lambda: union(_alphabetic(), _category("Nd")),
    "CNTRL": lambda: _category("Cc"),
    "DIGIT": lambda: _category("Nd"),
    "BLANK": _blank,
    "GRAPH": _graph,
    "PRINT": lambda: intersection(union(_graph(), _blank()), complement(_category("Cc"))),
    "ASCII": lambda: [(0, 0x7F)],
}

_BINARY = {
    "ALPHABETIC": _alphabetic,
    "LETTER": lambda: _category("L"),
    "LOWERCASE": lambda: _cased(True),
    "UPPERCASE": lambda: _cased(False),
    "TITLECASE": lambda: _category("Lt"),
    "PUNCTUATION": lambda: _category("P"),
    "CONTROL": lambda: _category("Cc"),
    "WHITESPACE": lambda: _WHITE_SPACE,
    "DIGIT": lambda: _category("Nd"),
    "HEXDIGIT": lambda: _HEX_DIGIT,
    "JOINCONTROL": lambda: [(0x200C, 0x200D)],
    "NONCHARACTERCODEPOINT": lambda: _NONCHARACTERS,
    "ASSIGNED": lambda: complement(_category("Cn")),
    "WORD": _word,
}

_GENERAL = {"LC": ("Lu", "Ll", "Lt"), "LD": ("L", "Nd"), "ALL": ("",)}


def _general(name: str) -> Ranges | None:
    """A general category or a group of them, `L`, `Lu`, `LC`, as Java names them; None for another name."""
    if name.upper() in _GENERAL:
        return _category(*_GENERAL[name.upper()])
    if name == "L1":
        return [(0, 0xFF)]
    if len(name) in (1, 2) and name[0] in "LMNPSZC" and (len(name) == 1 or name[1].islower()):
        ranges = _category(name)
        if ranges or name in ("Co", "Cs", "Cn", "Zl", "Zp"):
            return ranges
    return None


def property_ranges(name: str) -> Ranges:
    """The characters of the property `\\p{name}` names, as Java reads it with UNICODE_CHARACTER_CLASS."""
    for prefix in ("gc=", "general_category="):
        if name.startswith(prefix):
            found = _general(name[len(prefix) :])
            if found is None:
                break
            return found
    else:
        general = _general(name)
        if general is not None:
            return general
        if name.upper() in _NAMED:
            return _NAMED[name.upper()]()
        if name.startswith("Is"):
            bare = name[2:]
            binary = bare.upper().replace("_", "").replace(" ", "")
            if binary in _BINARY:
                return _BINARY[binary]()
            general = _general(bare)
            if general is not None:
                return general
    raise DrivineError(
        f"The regular expression's \\p{{{name}}} is not read on SQLite here: Python keeps no table of a script, a "
        "block or that property."
    )


def _point(cp: int) -> str:
    """The code point as an escape Python reads within a class and outside one."""
    if cp < 0x100:
        return f"\\x{cp:02x}"
    return f"\\u{cp:04x}" if cp < 0x10000 else f"\\U{cp:08x}"


def class_text(ranges: Ranges, negated: bool = False) -> str:
    """The ranges as a class Python reads: one that holds nothing never matches, and its negation matches anything."""
    if not ranges:
        return "[\\x00-\\U0010ffff]" if negated else "(?!)"
    held = "".join(_point(low) if low == high else f"{_point(low)}-{_point(high)}" for low, high in ranges)
    return f"[{'^' if negated else ''}{held}]"


_SPACES = " \t\n\x0b\f\r"
"""What Java reads past in an expression whose flags take in `x`."""


@dataclass
class _Frame:
    """A group of the expression as it is written: the flags in force in it, and those Python reads at its start."""

    flags: frozenset[str]
    opened_case: bool
    wrappers: int = 0


@dataclass
class JavaPattern:
    """A Java regular expression, written as Python's `re` reads it: see the module."""

    java: str
    at: int = 0
    out: list[str] = field(default_factory=list[str])
    groups: int = 0
    frames: list[_Frame] = field(default_factory=list[_Frame])

    def python(self) -> str:
        self.frames = [_Frame(frozenset(), False)]
        while self.at < len(self.java):
            self._next()
        # A group not closed is left so, for Python's own compile to refuse it as such.
        self.out.append("".join(")" * it.wrappers for it in reversed(self.frames)))
        return "".join(self.out)

    @property
    def _frame(self) -> _Frame:
        return self.frames[-1]

    def _refuse(self, because: str) -> None:
        raise DrivineError(f"The regular expression {self.java!r} is not read on SQLite here: {because}.")

    def _next(self) -> None:
        c = self.java[self.at]
        flags = self._frame.flags
        if "x" in flags and self._comment():
            return
        self.at += 1
        if c == "\\":
            self.out.append(self._escape())
        elif c == "[":
            negated, ranges = self._class()
            self.out.append(class_text(ranges, negated))
        elif c == "(":
            self._open()
        elif c == ")":
            if len(self.frames) == 1:
                self._refuse("a ')' closes no group")
            self.out.append(")" * self._frame.wrappers + ")")
            self.frames.pop()
        elif c == "|":
            frame = self._frame
            self.out.append(")" * frame.wrappers + "|")
            frame.wrappers = 0
            self._case(frame, "i" in frame.flags)
        elif c == ".":
            self.out.append(
                "[\\x00-\\U0010ffff]" if "s" in flags else "[^\\n]" if "d" in flags else f"[^{_LINE_TERMINATORS}]"
            )
        elif c == "^":
            if "m" not in flags:
                self.out.append("\\A")
            elif "d" in flags:
                self.out.append("(?:\\A|(?<=\\n)(?!\\Z))")
            else:
                self.out.append("(?:\\A|(?:(?<=[\\n\\x85\\u2028\\u2029])|(?<=\\r)(?!\\n))(?!\\Z))")
        elif c == "$":
            if "d" in flags:
                self.out.append("(?=\\n|\\Z)" if "m" in flags else "(?=\\n?\\Z)")
            elif "m" in flags:
                self.out.append(f"(?:(?=[{_LINE_TERMINATORS}]|\\Z)(?!(?<=\\r)\\n))")
            else:
                self.out.append(self._end_but_terminator())
        else:
            self.out.append(c)

    @staticmethod
    def _end_but_terminator() -> str:
        """The end of the text, or before a line terminator that ends it, as Java's `$` and `\\Z` are."""
        return f"(?:(?=(?:\\r\\n|[{_LINE_TERMINATORS}])?\\Z)(?!(?<=\\r)\\n))"

    def _comment(self) -> bool:
        """Reads past a space or a comment, where `x` is in force, as Java does: whether there was one."""
        c = self.java[self.at]
        if c in _SPACES:
            self.at += 1
            return True
        if c == "#":
            end = self.java.find("\n", self.at)
            self.at = len(self.java) if end < 0 else end + 1
            return True
        return False

    def _case(self, frame: _Frame, ignore: bool) -> None:
        """Opens a group of its own where the case is to be ignored, or not, and Python reads otherwise."""
        if ignore != frame.opened_case:
            self.out.append("(?i:" if ignore else "(?-i:")
            frame.wrappers += 1

    def _open(self) -> None:
        java = self.java
        flags = self._frame.flags
        if not java.startswith("?", self.at):
            self.groups += 1
            self.out.append("(")
            self.frames.append(_Frame(flags, "i" in flags))
            return
        self.at += 1
        rest = java[self.at :]
        for opening in (":", "=", "!", ">", "<=", "<!"):
            if rest.startswith(opening):
                self.at += len(opening)
                self.out.append(f"(?{opening}")
                self.frames.append(_Frame(flags, "i" in flags))
                return
        named = re.match(r"<([a-zA-Z][a-zA-Z0-9]*)>", rest)
        if named:
            self.at += named.end()
            self.groups += 1
            self.out.append(f"(?P<{named.group(1)}>")
            self.frames.append(_Frame(flags, "i" in flags))
            return
        found = re.match(r"([idmsuxUc]*)(?:-([idmsuxUc]*))?([:)])", rest)
        if not found:
            self._refuse(f"'(?{rest[:1]}' opens no group Java reads")
            return
        self.at += found.end()
        changed = (flags | set(found.group(1))) - set(found.group(2) or "")
        if found.group(3) == ")":
            frame = self._frame
            frame.flags = frozenset(changed)
            if ("i" in changed) != ("i" in flags):
                # Python's flags of the group so far stay as they are: the rest of it is a group of its own.
                self.out.append(")" * frame.wrappers)
                frame.wrappers = 0
                self._case(frame, "i" in changed)
            return
        ignore = "i" in changed
        self.out.append("(?:" if ignore == ("i" in flags) else "(?i:" if ignore else "(?-i:")
        self.frames.append(_Frame(frozenset(changed), ignore))

    def _escape(self) -> str:
        """The escape after a backslash, outside a class, as Python reads it."""
        java = self.java
        if self.at >= len(java):
            self._refuse("it ends with a backslash")
        c = java[self.at]
        self.at += 1
        flags = self._frame.flags
        if c == "z":
            return "\\Z"
        if c == "Z":
            return "(?=\\n?\\Z)" if "d" in flags else self._end_but_terminator()
        if c == "A":
            return "\\A"
        if c in "bB":
            if java.startswith("{", self.at):
                self._refuse("Python reads no boundary of a grapheme, \\b{g}")
            word = class_text(_word())
            if c == "b":
                return f"(?:(?<={word})(?!{word})|(?<!{word})(?={word}))"
            return f"(?:(?<={word})(?={word})|(?<!{word})(?!{word}))"
        if c == "R":
            return "(?>\\r\\n|[\\n\\x0b\\x0c\\r\\x85\\u2028\\u2029])"
        if c == "Q":
            end = java.find("\\E", self.at)
            literal = java[self.at :] if end < 0 else java[self.at : end]
            self.at = len(java) if end < 0 else end + 2
            return re.escape(literal)
        if c == "k":
            named = re.match(r"<([a-zA-Z][a-zA-Z0-9]*)>", java[self.at :])
            if not named:
                self._refuse("\\k names no group")
                return ""
            self.at += named.end()
            return f"(?P={named.group(1)})"
        if c in "123456789":
            number = int(c)
            while self.at < len(java) and java[self.at].isdigit() and self.groups >= number * 10 + int(java[self.at]):
                number = number * 10 + int(java[self.at])
                self.at += 1
            return f"(?:\\{number})" if number <= self.groups else "(?!)"
        ranges = self._class_escape(c)
        if ranges is not None:
            return class_text(*ranges)
        cp = self._code_point(c)
        return re.escape(chr(cp))

    def _class_escape(self, c: str) -> tuple[Ranges, bool] | None:
        """The class an escape names, `\\w`, `\\p{L}`, and whether it is negated; None for another escape."""
        named = {"w": _word, "s": lambda: _WHITE_SPACE, "d": lambda: _category("Nd"), "h": lambda: _HORIZONTAL}
        named["v"] = lambda: _VERTICAL
        if c.lower() in named:
            return named[c.lower()](), c.isupper()
        if c in "pP":
            java = self.java
            if java.startswith("{", self.at):
                end = java.find("}", self.at)
                if end < 0:
                    self._refuse("a \\p{ is not closed")
                name = java[self.at + 1 : end]
                self.at = end + 1
            elif self.at < len(java):
                name = java[self.at]
                self.at += 1
            else:
                self._refuse("it ends with \\p")
                return None
            ranges = property_ranges(name)
            return ranges, c == "P"
        return None

    def _code_point(self, c: str) -> int:
        """The character an escape of one stands for, after its backslash and `c`."""
        java = self.java
        if c == "x":
            if java.startswith("{", self.at):
                end = java.find("}", self.at)
                digits = java[self.at + 1 : end] if end > 0 else ""
                self.at = end + 1
            else:
                digits = java[self.at : self.at + 2]
                self.at += 2
            if not re.fullmatch(r"[0-9a-fA-F]+", digits) or int(digits, 16) > MAX_CODE_POINT:
                self._refuse("\\x names no character")
            return int(digits, 16)
        if c == "u":
            digits = java[self.at : self.at + 4]
            if not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                self._refuse("\\u names no character")
            self.at += 4
            cp = int(digits, 16)
            low = re.match(r"\\u(d[c-f][0-9a-f]{2})", java[self.at : self.at + 6], re.IGNORECASE)
            if 0xD800 <= cp <= 0xDBFF and low:
                # Two halves of a character outside the basic plane, as Java reads them, one character.
                self.at += 6
                return 0x10000 + ((cp - 0xD800) << 10) + (int(low.group(1), 16) - 0xDC00)
            return cp
        if c == "0":
            digits = re.match(r"[0-3][0-7]{2}|[0-7]{1,2}", java[self.at :])
            if not digits:
                self._refuse("\\0 is not followed by an octal digit")
                return 0
            self.at += digits.end()
            return int(digits.group(0), 8)
        if c == "c":
            if self.at >= len(java):
                self._refuse("it ends with \\c")
            self.at += 1
            return ord(java[self.at - 1]) ^ 64
        if c == "N":
            named = re.match(r"\{([^}]*)\}", java[self.at :])
            try:
                cp = ord(unicodedata.lookup(named.group(1))) if named else -1
            except KeyError:
                cp = -1
            if not named or cp < 0:
                self._refuse("\\N names no character")
            self.at += named.end() if named else 0
            return cp
        simple = {"t": 0x09, "n": 0x0A, "r": 0x0D, "f": 0x0C, "a": 0x07, "e": 0x1B}
        if c in simple:
            return simple[c]
        if c.isascii() and c.isalnum():
            self._refuse(f"Java reads no escape \\{c}")
        return ord(c)

    def _class(self) -> tuple[bool, Ranges]:
        """The class at a `[`, read past it, as the ranges it holds, and whether it is negated."""
        negated = self.java.startswith("^", self.at)
        if negated:
            self.at += 1
        return negated, self._class_items(first=True)

    def _class_items(self, first: bool) -> Ranges:
        """The items of a class up to its `]`, read past it: a union, and an intersection where `&&` joins two."""
        java = self.java
        held: Ranges = []
        start = self.at
        while True:
            if self.at >= len(java):
                self._refuse("a class is not closed")
            if "x" in self._frame.flags and self._comment():
                continue
            c = java[self.at]
            if c == "]" and not first:
                self.at += 1
                return held
            first = False
            if java.startswith("&&", self.at):
                self.at += 2
                # An intersection with nothing on one side, `[a&&]` or `[&&a]`, is the other side, as Java reads it.
                before = self.at - 2 > start
                after = not java.startswith("]", self.at)
                other = self._class_items(first=False)
                return intersection(held, other) if before and after else other if after else held
            if c == "[":
                self.at += 1
                negated, inner = self._class()
                held = union(held, complement(inner) if negated else inner)
                continue
            self.at += 1
            if c == "\\":
                if self.at >= len(java):
                    self._refuse("it ends with a backslash")
                e = java[self.at]
                self.at += 1
                if e == "Q":
                    end = java.find("\\E", self.at)
                    literal = java[self.at :] if end < 0 else java[self.at : end]
                    self.at = len(java) if end < 0 else end + 2
                    held = union(held, [(ord(it), ord(it)) for it in literal])
                    continue
                named = self._class_escape(e)
                if named is not None:
                    ranges, negated = named
                    held = union(held, complement(ranges) if negated else ranges)
                    continue
                low = self._code_point(e)
            else:
                low = ord(c)
            high = low
            if java.startswith("-", self.at) and self.at + 1 < len(java) and java[self.at + 1] not in "][":
                self.at += 1
                end = java[self.at]
                self.at += 1
                if end == "\\" and self.at < len(java):
                    self.at += 1
                    high = self._code_point(java[self.at - 1])
                else:
                    high = ord(end)
                if high < low:
                    self._refuse(f"the range {chr(low)}-{chr(high)} is backwards")
            held = union(held, [(low, high)])


def python_pattern(java: str) -> str:
    """The Java regular expression, as Python's `re` reads it: see the module."""
    for posix, name in _POSIX.items():
        java = java.replace(posix, name)
    return JavaPattern(java).python()


class Patterns:
    """
    The regular expressions the functions of one connection have read, compiled once each: a search asks the same few
    of every node it reads.
    """

    _MAX = 256

    def __init__(self) -> None:
        self._compiled: OrderedDict[str, re.Pattern[str]] = OrderedDict()

    def of(self, expression: str) -> re.Pattern[str]:
        compiled = self._compiled.get(expression)
        if compiled is None:
            compiled = re.compile(python_pattern(expression))
            self._compiled[expression] = compiled
            if len(self._compiled) > self._MAX:
                self._compiled.popitem(last=False)
        else:
            self._compiled.move_to_end(expression)
        return compiled
