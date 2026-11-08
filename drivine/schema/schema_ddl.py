"""
Reads Neo4j's schema DDL as Drivine's schema specs: a pure function of the text, which touches no store. drivine4j's
`SchemaDdl`, ported.

Neo4j's schema commands are not Cypher queries (openCypher's grammar has no rules for them), so they are read here, by
hand, and not by the Cypher reader. A statement is taken for DDL only where it opens with one of these sequences of
keywords, so that Cypher that merely uses the words, such as `CREATE index = (a)-[:R]->(b)` or a variable named `index`,
reads as Cypher still (`NotDdl`):

- `CREATE [RANGE | BTREE | TEXT | FULLTEXT | VECTOR | POINT | LOOKUP] INDEX`, not followed by `=`;
- `CREATE CONSTRAINT`, not followed by `=`;
- `DROP INDEX` and `DROP CONSTRAINT`;
- `CALL db.awaitIndexes` and `CALL db.awaitIndex`.

What it reads:

- RangeIndexSpec: `CREATE [RANGE] INDEX [name] [IF NOT EXISTS] FOR (n:L) ON (n.a, n.b)`, or Neo4j 4's `ON :L(a, b)`;
- UniquenessConstraintSpec: `CREATE CONSTRAINT [name] [IF NOT EXISTS] FOR (n:L) REQUIRE n.p IS [NODE] UNIQUE`,
  `REQUIRE (n.a, n.b) IS UNIQUE`, or Neo4j 4's `ON (n:L) ASSERT n.p IS UNIQUE`;
- FullTextIndexSpec: `CREATE FULLTEXT INDEX … FOR (n:L) ON EACH [n.a, …] [OPTIONS {indexConfig:
  {`fulltext.analyzer`: …}}]`;
- VectorIndexSpec: `CREATE VECTOR INDEX … FOR (n:L) ON n.p OPTIONS {indexConfig: {`vector.dimensions`: …}}`;
- TextIndexSpec, which only Neo4j makes: `CREATE TEXT INDEX … FOR (n:L) ON (n.p)`;
- Drop: `DROP INDEX name [IF EXISTS]`, `DROP CONSTRAINT name [IF EXISTS]`;
- AwaitIndexes: `CALL db.awaitIndexes(…)`, `CALL db.awaitIndex(…)`.

A name is a plain identifier or one in backticks, a doubled backtick in it standing for one. Keywords are read in any
case. A statement may end with a `;`.

What a SQL store cannot honour is refused by name (`Refused`): POINT and LOOKUP indexes, relationship indexes and
constraints, an index or constraint over several labels, existence (`IS NOT NULL`), key (`IS NODE KEY`) and type
(`IS :: TYPE`) constraints, a vector index over several properties or `WITH` properties to filter by, and one with no
`vector.dimensions`; Neo4j 4's `DROP … ON` by label and properties; a name or `OPTIONS` given as a parameter; and any
schema for the labels of Drivine's own nodes but their own uniqueness constraints, which read as `Reserved`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from drivine.backend.cypher_backend import SCHEMA_MARKER, STORE_LABEL
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.schema.engine_vector_options import EngineVectorOptions
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_ddl_result import (
    AWAIT_INDEXES,
    NOT_DDL,
    Create,
    DdlSpec,
    Drop,
    Refused,
    Reserved,
    SchemaDdlResult,
    TextIndexSpec,
    default_name,
)
from drivine.schema.schema_specs import uniqueness_constraint, vector_index
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import SimilarityFunction, VectorIndexSpec

STORES = (DatabaseType.POSTGRES, DatabaseType.ORACLE, DatabaseType.SQLITE)
"""The stores whose Cypher is compiled to SQL, where Drivine runs schema DDL itself."""

SCHEMA_COMMAND = (
    "The statement is a schema command, which is not compiled to SQL: run it through a PersistenceManager, which "
    "applies it to the store's schema."
)
"""Why the compiler refuses a schema command a SQL store honours: it is no query, and a PersistenceManager runs it."""

_RESERVED: dict[str, UniquenessConstraintSpec] = {
    SCHEMA_MARKER: uniqueness_constraint(SCHEMA_MARKER, "scope"),
    STORE_LABEL: uniqueness_constraint(STORE_LABEL, "key"),
}
"""The uniqueness constraints of Drivine's own nodes, by label: the schema marker's scope and the store stamp's key."""

_INDEX_KINDS = {"RANGE", "BTREE", "TEXT", "FULLTEXT", "VECTOR", "POINT", "LOOKUP"}
"""The kinds of index a `CREATE … INDEX` names before `INDEX`."""


def translate(text: str) -> SchemaDdlResult:
    """What the text is: schema DDL as a spec, a refusal, or not DDL."""
    reader = _Reader(text)
    try:
        if reader.word(0, "CREATE"):
            return reader.create()
        if reader.word(0, "DROP") and (reader.word(1, "INDEX") or reader.word(1, "CONSTRAINT")):
            return reader.drop()
        if reader.word(0, "CALL"):
            return reader.call()
        return NOT_DDL
    except _Refusal as refusal:
        return Refused(refusal.message)


def reserved(spec: DdlSpec) -> bool:
    """Whether the spec is the uniqueness constraint of one of Drivine's own nodes, by any name."""
    own = _RESERVED.get(spec.label)
    return isinstance(spec, UniquenessConstraintSpec) and own is not None and own.properties == spec.properties


def ddl_of(type: DatabaseType, spec: QuerySpecification[Any]) -> SchemaDdlResult | None:
    """
    The schema DDL the specification is, run on a store of `type`: None where it is none, or where the store runs Cypher
    itself and is sent the statement as it is, or where the statement is not Cypher.
    """
    if type not in STORES:
        return None
    statement = spec.statement
    if statement is None or statement.language != "CYPHER":
        return None
    ddl = translate(statement.text)
    return None if ddl is NOT_DDL else ddl


def refused_in_transaction(type: DatabaseType, spec: QuerySpecification[Any]) -> bool:
    """
    Whether the specification, run on a store of `type` inside an open transaction, is `db.awaitIndexes`, which does
    nothing there; raises for any other schema DDL, which Drivine runs on connections of its own and the transaction's
    rollback would not undo. False for a statement that is no DDL.
    """
    ddl = ddl_of(type, spec)
    if ddl is None:
        return False
    if ddl is AWAIT_INDEXES:
        return True
    if isinstance(ddl, Refused):
        raise DrivineError(ddl.message, spec)
    engine = "SQLite" if type is DatabaseType.SQLITE else type.value.capitalize()
    raise DrivineError(
        f"A schema command is not run inside a transaction on {engine}: Drivine applies it on connections of its own, "
        "which the transaction's rollback would not undo. Run it outside the transaction, with a non-transactional "
        "PersistenceManager.",
        spec,
    )


class _Refusal(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_Type = Literal["WORD", "NAME", "STRING", "NUMBER", "PARAMETER", "SYMBOL", "END", "BAD"]


@dataclass(frozen=True)
class _Token:
    """A token: a WORD is a bare identifier or keyword, a NAME one in backticks, `text` what it reads as."""

    type: _Type
    text: str
    value: Any = None

    def __str__(self) -> str:
        if self.type == "END":
            return "the end"
        if self.type == "NAME":
            return f"`{self.text}`"
        if self.type == "PARAMETER":
            return f"${self.text}"
        return f"'{self.text}'"


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


class _Lexer:
    """Reads the text into tokens as they are asked for, so that a statement that is not DDL is read no further than
    its first few."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.at = 0

    def next(self) -> _Token:
        self._skip_space()
        text = self.text
        if self.at >= len(text):
            return _Token("END", "")
        c = text[self.at]
        if c == "`":
            return self._quoted()
        if c in ("'", '"'):
            return self._string(c)
        if c.isdecimal():
            return self._number()
        if c.isalpha() or c == "_":
            return _Token("WORD", self._word())
        if c == "$":
            return self._parameter()
        if text.startswith("::", self.at):
            self.at += 2
            return _Token("SYMBOL", "::")
        self.at += 1
        return _Token("SYMBOL", c)

    def _skip_space(self) -> None:
        text = self.text
        while self.at < len(text):
            if text[self.at].isspace():
                self.at += 1
            elif text.startswith("//", self.at):
                end = text.find("\n", self.at)
                self.at = len(text) if end < 0 else end + 1
            elif text.startswith("/*", self.at):
                end = text.find("*/", self.at + 2)
                self.at = len(text) if end < 0 else end + 2
            else:
                return

    def _word(self) -> str:
        start = self.at
        # Letters and decimal digits, as drivine4j's isLetterOrDigit reads them: not a superscript, which is no digit.
        while self.at < len(self.text) and (
            self.text[self.at].isalpha() or self.text[self.at].isdecimal() or self.text[self.at] == "_"
        ):
            self.at += 1
        return self.text[start : self.at]

    def _quoted(self) -> _Token:
        """A name in backticks, a doubled backtick in it standing for one."""
        name: list[str] = []
        text = self.text
        self.at += 1
        while self.at < len(text):
            c = text[self.at]
            self.at += 1
            if c != "`":
                name.append(c)
            elif self.at < len(text) and text[self.at] == "`":
                name.append("`")
                self.at += 1
            else:
                return _Token("NAME", "".join(name))
        return _Token("BAD", "A name in backticks has no closing backtick.")

    def _string(self, quote: str) -> _Token:
        value: list[str] = []
        text = self.text
        self.at += 1
        while self.at < len(text):
            c = text[self.at]
            self.at += 1
            if c == quote:
                written = "".join(value)
                return _Token("STRING", written, written)
            if c == "\\" and self.at < len(text):
                escaped = text[self.at]
                self.at += 1
                if escaped == "u":
                    digits = text[self.at : self.at + 4]
                    if len(digits) == 4 and all(it in "0123456789abcdefABCDEF" for it in digits):
                        value.append(chr(int(digits, 16)))
                        self.at += 4
                    else:
                        value.append("u")
                else:
                    value.append(_ESCAPES.get(escaped, escaped))
            else:
                value.append(c)
        return _Token("BAD", "A string has no closing quote.")

    def _number(self) -> _Token:
        text = self.text
        start = self.at
        while self.at < len(text) and text[self.at].isdecimal():
            self.at += 1
        fraction = False
        if self.at + 1 < len(text) and text[self.at] == "." and text[self.at + 1].isdecimal():
            fraction = True
            self.at += 1
            while self.at < len(text) and text[self.at].isdecimal():
                self.at += 1
        if self.at < len(text) and text[self.at] in "eE":
            mark = self.at
            self.at += 1
            if self.at < len(text) and text[self.at] in "+-":
                self.at += 1
            if self.at < len(text) and text[self.at].isdecimal():
                fraction = True
                while self.at < len(text) and text[self.at].isdecimal():
                    self.at += 1
            else:
                self.at = mark
        written = text[start : self.at]
        value: int | float = float(written) if fraction else int(written)
        return _Token("NUMBER", written, value)

    def _parameter(self) -> _Token:
        self.at += 1
        if self.at < len(self.text) and self.text[self.at] == "`":
            return _Token("PARAMETER", self._quoted().text)
        return _Token("PARAMETER", self._word())


def _written(value: Any) -> str:
    """A value as Cypher writes it, in a refusal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class _Reader:
    """Reads one statement's tokens, from its first."""

    def __init__(self, text: str) -> None:
        self.lexer = _Lexer(text)
        self.tokens: list[_Token] = []
        self.position = 0

    def peek(self, ahead: int = 0) -> _Token:
        while len(self.tokens) <= self.position + ahead:
            last = self.tokens[-1] if self.tokens else None
            self.tokens.append(last if last is not None and last.type in ("END", "BAD") else self.lexer.next())
        return self.tokens[self.position + ahead]

    def _advance(self) -> _Token:
        token = self.peek()
        self.position += 1
        return token

    def word(self, ahead: int, keyword: str) -> bool:
        token = self.peek(ahead)
        return token.type == "WORD" and token.text.upper() == keyword.upper()

    def _is(self, keyword: str) -> bool:
        return self.word(0, keyword)

    def _symbol(self, symbol: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token.type == "SYMBOL" and token.text == symbol

    def _accept(self, symbol: str) -> bool:
        if self._symbol(symbol):
            self._advance()
            return True
        return False

    def _fail(self, expected: str) -> NoReturn:
        found = self.peek()
        if found.type == "BAD":
            raise _Refusal(f"Invalid schema command: {found.text}")
        raise _Refusal(f"Invalid schema command: expected {expected}, but found {found}.")

    def _expect_word(self, keyword: str) -> None:
        if not self._is(keyword):
            self._fail(keyword)
        self._advance()

    def _expect_symbol(self, symbol: str) -> None:
        if not self._symbol(symbol):
            self._fail(f"'{symbol}'")
        self._advance()

    def _name(self, what: str) -> str:
        """A label, a property or a variable: bare, or in backticks."""
        token = self.peek()
        if token.type not in ("WORD", "NAME"):
            self._fail(what)
        self._advance()
        if not token.text:
            raise _Refusal(f"A {what} is empty: an empty name names nothing.")
        return token.text

    def _end(self) -> None:
        """The end of the statement, after an optional `;`."""
        self._accept(";")
        if self.peek().type != "END":
            self._fail("the end of the schema command, which runs on its own")

    # ----- CREATE -----

    def create(self) -> SchemaDdlResult:
        first = self.peek(1)
        if first.type != "WORD":
            return NOT_DDL
        kind = first.text.upper()
        if kind in ("INDEX", "CONSTRAINT") and not self._symbol("=", 2):
            self.position += 2
            return self._index("") if kind == "INDEX" else self._constraint()
        if kind in _INDEX_KINDS and self.word(2, "INDEX") and not self._symbol("=", 3):
            self.position += 3
            return self._index(kind)
        return NOT_DDL

    def _item_name(self) -> str | None:
        """The item's name, when it has one: not the `IF NOT EXISTS`, `FOR (` or `ON :` that follows where it has
        none."""
        token = self.peek()
        if token.type == "PARAMETER":
            raise _Refusal(
                f"A schema item named by a parameter, ${token.text}, is not created on a SQL store: write its name."
            )
        if token.type == "WORD":
            keyword = token.text.upper()
            if keyword == "IF" and self.word(1, "NOT"):
                return None
            if keyword == "FOR" and self._symbol("(", 1):
                return None
            if keyword == "ON" and (self._symbol(":", 1) or self._symbol("(", 1)):
                return None
        elif token.type != "NAME":
            return None
        self._advance()
        if not token.text.strip():
            raise _Refusal("A schema item's name is empty: an item is created with a name, or with none.")
        return token.text

    def _if_not_exists(self) -> bool:
        if not self._is("IF"):
            return False
        self._advance()
        self._expect_word("NOT")
        self._expect_word("EXISTS")
        return True

    def _node_pattern(self, item: str) -> tuple[str | None, str]:
        """The node pattern an item is for, `(n:L)`: its variable, which may be left out, and its one label."""
        self._expect_symbol("(")
        variable = self._name("variable") if self.peek().type in ("WORD", "NAME") else None
        if self._symbol(")"):
            self._advance()
            if self._symbol("-") or self._symbol("<"):
                raise _relationships(item)
            raise _Refusal(f"{item} is made for the nodes of one label, and this pattern names none.")
        self._expect_symbol(":")
        label = self._name("label")
        if self._symbol("|") or self._symbol(":") or self._symbol("&"):
            raise _Refusal(f"{item} over several labels is not made on a SQL store: declare one for each label.")
        self._expect_symbol(")")
        if self._symbol("-") or self._symbol("<"):
            raise _relationships(item)
        return variable, label

    def _property(self, variable: str | None) -> str:
        """A property of the pattern's node, `n.p`."""
        owner = self._name("variable")
        if variable is not None and owner != variable:
            raise _Refusal(f"The property {owner}.… is of {owner}, and the pattern binds {variable}.")
        self._expect_symbol(".")
        return self._name("property")

    def _properties(self, variable: str | None, open: str, close: str) -> list[str]:
        """Properties of the pattern's node, between `open` and `close`, separated by commas."""
        self._expect_symbol(open)
        properties = [self._property(variable)]
        while self._accept(","):
            properties.append(self._property(variable))
        self._expect_symbol(close)
        return properties

    def _index(self, kind: str) -> SchemaDdlResult:
        if kind == "POINT":
            raise _Refusal("A POINT index is Neo4j's, and is not made on a SQL store.")
        if kind == "LOOKUP":
            raise _Refusal(
                "A LOOKUP index is Neo4j's, and is not made on a SQL store, which finds a node's labels without one."
            )
        item = {"FULLTEXT": "A full-text index", "VECTOR": "A vector index", "TEXT": "A TEXT index"}.get(
            kind, "An index"
        )
        name = self._item_name()
        if_not_exists = self._if_not_exists()
        if self._is("FOR"):
            self._advance()
            variable, label = self._node_pattern(item)
            self._expect_word("ON")
            if kind == "FULLTEXT":
                self._expect_word("EACH")
                properties = self._properties(variable, "[", "]")
            elif self._symbol("("):
                properties = self._properties(variable, "(", ")")
            else:
                properties = [self._property(variable)]
        elif self._is("ON") and self._symbol(":", 1) and kind not in ("FULLTEXT", "VECTOR"):
            # Neo4j 4's CREATE INDEX ON :L(a, b), whose properties are named bare.
            self._advance()
            self._advance()
            label = self._name("label")
            self._expect_symbol("(")
            properties = [self._name("property")]
            while self._accept(","):
                properties.append(self._name("property"))
            self._expect_symbol(")")
        else:
            self._fail("FOR")
        if kind == "VECTOR" and self._is("WITH"):
            raise _Refusal("A vector index WITH properties to filter by is not made on a SQL store.")
        options = self._options()
        self._end()
        spec: DdlSpec
        if kind == "TEXT":
            spec = TextIndexSpec(label, (_single(item, properties),))
        elif kind == "FULLTEXT":
            spec = FullTextIndexSpec(label, tuple(properties), analyzer=_analyzer(options))
        elif kind == "VECTOR":
            spec = _vector(label, _single(item, properties), options)
        else:
            spec = RangeIndexSpec(label, tuple(properties))
        return _created(spec, name, if_not_exists)

    def _constraint(self) -> SchemaDdlResult:
        name = self._item_name()
        if_not_exists = self._if_not_exists()
        if self._is("FOR"):
            neo4j4 = False
        elif self._is("ON"):
            neo4j4 = True
        else:
            self._fail("FOR")
        self._advance()
        item = "A constraint"
        variable, label = self._node_pattern(item)
        self._expect_word("ASSERT" if neo4j4 else "REQUIRE")
        if self._is("EXISTS") and self._symbol("(", 1):
            raise _existence()
        properties = self._properties(variable, "(", ")") if self._symbol("(") else [self._property(variable)]
        self._expect_word("IS")
        if self._is("NOT"):
            raise _existence()
        if self._symbol("::") or self._is("TYPED"):
            raise _Refusal(
                "A property type constraint (IS :: TYPE) is not made on a SQL store: Drivine's schema has uniqueness "
                "constraints only."
            )
        if self._is("KEY"):
            raise _key()
        if self._is("RELATIONSHIP") or self._is("REL"):
            raise _relationships(item)
        if self._is("NODE"):
            self._advance()
            if self._is("KEY"):
                raise _key()
            if not self._is("UNIQUE"):
                self._fail("UNIQUE")
            self._advance()
        elif self._is("UNIQUE"):
            self._advance()
        else:
            self._fail("UNIQUE")
        self._options()
        self._end()
        return _created(UniquenessConstraintSpec(label, tuple(properties)), name, if_not_exists)

    # ----- OPTIONS -----

    def _options(self) -> dict[str, Any]:
        """The map of `OPTIONS`, when there is one."""
        if not self._is("OPTIONS"):
            return {}
        self._advance()
        if self.peek().type == "PARAMETER":
            raise _Refusal("OPTIONS given as a parameter are not read on a SQL store: write them.")
        return self._map()

    def _map(self) -> dict[str, Any]:
        self._expect_symbol("{")
        entries: dict[str, Any] = {}
        if not self._symbol("}"):
            while True:
                key = self.peek()
                if key.type not in ("WORD", "NAME", "STRING"):
                    self._fail("a key")
                self._advance()
                self._expect_symbol(":")
                entries[key.text] = self._value()
                if not self._accept(","):
                    break
        self._expect_symbol("}")
        return entries

    def _value(self) -> Any:
        token = self.peek()
        if token.type in ("STRING", "NUMBER"):
            return self._advance().value
        if self._symbol("-") and self.peek(1).type == "NUMBER":
            self._advance()
            return -self._advance().value
        if self._is("true"):
            self._advance()
            return True
        if self._is("false"):
            self._advance()
            return False
        if self._is("null"):
            self._advance()
            return None
        if self._symbol("{"):
            return self._map()
        if self._symbol("["):
            self._advance()
            items: list[Any] = []
            if not self._symbol("]"):
                items.append(self._value())
                while self._accept(","):
                    items.append(self._value())
            self._expect_symbol("]")
            return items
        if token.type == "PARAMETER":
            raise _Refusal(f"An option given as a parameter, ${token.text}, is not read on a SQL store: write it.")
        self._fail("a value")

    # ----- DROP -----

    def drop(self) -> SchemaDdlResult:
        self._advance()
        kind: Literal["INDEX", "CONSTRAINT"] = "INDEX" if self._advance().text.upper() == "INDEX" else "CONSTRAINT"
        what = kind.lower()
        if self._is("ON") and (self._symbol(":", 1) or self._symbol("(", 1)):
            raise _Refusal(
                f"DROP {kind} ON … is Neo4j 4's, and drops by label and properties: drop the {what} by its name."
            )
        token = self.peek()
        if token.type == "PARAMETER":
            item = "An index" if kind == "INDEX" else "A constraint"
            raise _Refusal(
                f"{item} named by a parameter, ${token.text}, is not dropped on a SQL store: write its name."
            )
        name = self._name("name")
        if_exists = False
        if self._is("IF"):
            self._advance()
            self._expect_word("EXISTS")
            if_exists = True
        self._end()
        return Drop(kind, name, if_exists)

    # ----- CALL -----

    def call(self) -> SchemaDdlResult:
        procedure = self.peek(3)
        first = self.peek(1)
        awaits = (
            first.type == "WORD"
            and first.text == "db"
            and self._symbol(".", 2)
            and procedure.type == "WORD"
            and procedure.text in ("awaitIndexes", "awaitIndex")
        )
        if not awaits:
            return NOT_DDL
        self.position += 4
        if self._accept("("):
            if not self._symbol(")"):
                while True:
                    if self.peek().type == "PARAMETER":
                        self._advance()
                    else:
                        self._value()
                    if not self._accept(","):
                        break
            self._expect_symbol(")")
        self._end()
        return AWAIT_INDEXES


def _relationships(item: str) -> _Refusal:
    return _Refusal(f"{item} on relationships is not made on a SQL store: Drivine's schema is of nodes.")


def _existence() -> _Refusal:
    return _Refusal(
        "A property existence constraint (IS NOT NULL) is not made on a SQL store: Drivine's schema has uniqueness "
        "constraints only."
    )


def _key() -> _Refusal:
    return _Refusal(
        "A key constraint (IS NODE KEY) is not made on a SQL store: Drivine's schema has uniqueness constraints only."
    )


def _single(item: str, properties: list[str]) -> str:
    if len(properties) != 1:
        raise _Refusal(f"{item} is over one property, and this names {len(properties)}.")
    return properties[0]


def _created(spec: DdlSpec, name: str | None, if_not_exists: bool) -> SchemaDdlResult:
    """
    The spec, as DDL named it: Drivine's own, or one to create, with the name as its alias where the schema API names it
    otherwise.
    """
    own = _RESERVED.get(spec.label)
    if own is not None:
        if reserved(spec):
            return Reserved(own)
        raise _Refusal(f"{spec.label} is the label of Drivine's own nodes, and no schema is made for it but Drivine's.")
    return Create(spec, name if name != default_name(spec) else None, if_not_exists)


def _config(options: dict[str, Any]) -> dict[str, Any]:
    config = options.get("indexConfig")
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise _Refusal("OPTIONS indexConfig is a map.")
    return config  # pyright: ignore[reportUnknownVariableType]


def _analyzer(options: dict[str, Any]) -> str | None:
    analyzer = _config(options).get("fulltext.analyzer")
    if analyzer is not None and not isinstance(analyzer, str):
        raise _Refusal(f"A full-text index's `fulltext.analyzer` is the name of an analyzer, not {_written(analyzer)}.")
    return analyzer


def _vector(label: str, property: str, options: dict[str, Any]) -> VectorIndexSpec:
    config = _config(options)
    dimensions = _whole(config, "vector.dimensions")
    if dimensions is None:
        raise _Refusal(
            "A vector index on a SQL store needs `vector.dimensions` in its OPTIONS indexConfig: nothing else gives "
            "the size of its embeddings."
        )
    function = config.get("vector.similarity_function")
    similarity: SimilarityFunction
    if function is None:
        similarity = "COSINE"
    elif isinstance(function, str) and function.upper() in ("COSINE", "EUCLIDEAN"):
        similarity = "COSINE" if function.upper() == "COSINE" else "EUCLIDEAN"
    else:
        written = f"'{function}'" if isinstance(function, str) else _written(function)
        raise _Refusal(f"A vector index's `vector.similarity_function` is 'cosine' or 'euclidean', not {written}.")
    quantization = config.get("vector.quantization.enabled")
    if quantization is not None and not isinstance(quantization, bool):
        raise _Refusal(
            f"A vector index's `vector.quantization.enabled` is true or false, not {_written(quantization)}."
        )
    return vector_index(
        label,
        property,
        dimensions,
        similarity,
        hnsw_m=_whole(config, "vector.hnsw.m"),
        hnsw_ef_construction=_whole(config, "vector.hnsw.ef_construction"),
        engine_options=(
            [EngineVectorOptions("Neo4j", quantization_enabled=quantization)] if quantization is not None else []
        ),
    )


_INT_MAX = 2**31 - 1


def _whole(config: dict[str, Any], key: str) -> int | None:
    """The positive whole number the key holds in the config; None where it holds none."""
    value = config.get(key)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _INT_MAX:
        return value
    if isinstance(value, float) and 1 <= value <= _INT_MAX and value.is_integer():
        return int(value)
    raise _Refusal(f"A vector index's `{key}` is a positive whole number, not {_written(value)}.")
