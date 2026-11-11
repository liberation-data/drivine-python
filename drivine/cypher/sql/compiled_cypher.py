from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompiledCypher:
    """
    A Cypher statement as SQL: the statement, and the values its parameters are bound to. Where the statement must be
    readied first, `setup` holds the statements that ready it, each run before it, in order, with no parameters: on
    Postgres, the function a statement that writes is compiled to, which psycopg sends apart from the statement that
    calls it, as a statement with parameters is one command.

    `calls_stored_nulls_equal` says whether the statement calls the function an Oracle graph is made with,
    `<graph>_nulls_equal`, which a store made by an earlier version, or by a user who may not make a function, does not
    have: see `OracleCypherCompiler.nulls_equal`. `calls_stored_era` says the same of the functions it renumbers a year
    that may be BC by, `<graph>_era` and `<graph>_era_date`: see `OracleTemporalSql.era`.

    `non_finite` says whether the statement was given NaN or an infinity, so its rows may hold the numbers that stand
    for them, as drivine4j's compiler writes them on Postgres: see postgres_non_finite. Drivine's own compilers say
    False of every statement, as they wrote NaN and the infinities as text (liberation-data/drivine#673); the shared
    compiler says it of a statement given one.
    """

    sql: str
    parameters: dict[str, Any]
    setup: tuple[str, ...] = ()
    calls_stored_nulls_equal: bool = False
    calls_stored_era: bool = False
    non_finite: bool = False

    @property
    def prelude(self) -> str | None:
        """
        The setup as one text, for the tests of Drivine's own compilers (drivine/cypher/sql), which go with them when
        the shared compiler takes their place (liberation-data/drivine-python#471, stage 3). Nothing else reads it.
        """
        return "\n".join(self.setup) or None
