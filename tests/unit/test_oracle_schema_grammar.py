"""Oracle's schema statements where the golden statements hold none of the case, the catalog's key, and its items."""

from typing import Any

import pytest

from drivine import range_index
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.oracle.oracle_schema_grammar import oracle_schema_grammar
from drivine.backend.sql.sql_catalog import catalog_properties, catalog_record
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.dsl.query_index_advisor import QueryIndexAdvisor
from drivine.schema.schema_executor import SchemaExecutor
from drivine.schema.schema_item import SchemaItem

GRAMMAR = oracle_schema_grammar(OracleGraph("jvm"))


def shared_index(prop: str) -> str:
    """The DDL of the index over the property of every node, as the block that makes it holds it."""
    return GRAMMAR.create_range(range_index("Person", prop))[0]


def test_writes_the_shared_index_path_as_drivine4j_bare_for_a_key_sql_takes_for_an_identifier():
    assert "(JSON_VALUE(props, ''$.name''))" in shared_index("name")
    assert "(JSON_VALUE(props, ''$._created2''))" in shared_index("_created2")


def test_quotes_a_key_drivine4j_quotes_one_a_json_path_reads_as_a_value_or_that_is_not_an_identifier():
    # drivine4j: `'$."true"'`, `'$."meta.source"'`, `'$."it''s"'`, `'$."a\"b"'`, then doubled for EXECUTE IMMEDIATE.
    assert "(JSON_VALUE(props, ''$.\"true\"''))" in shared_index("true")
    assert "(JSON_VALUE(props, ''$.\"null\"''))" in shared_index("null")
    assert "(JSON_VALUE(props, ''$.\"meta.source\"''))" in shared_index("meta.source")
    assert "(JSON_VALUE(props, ''$.\"2fa\"''))" in shared_index("2fa")
    assert "(JSON_VALUE(props, ''$.\"it''''s\"''))" in shared_index("it's")
    assert "(JSON_VALUE(props, ''$.\"a\\\"b\"''))" in shared_index('a"b')


class _Failing:
    """A provider whose connections fail each statement with the next of `failures`, then return no rows."""

    type = DatabaseType.ORACLE
    name = "oracle"

    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        self.runs = 0

    async def connect(self) -> Any:
        provider = self

        class Connection:
            async def query(self, spec: Any) -> list[Any]:
                provider.runs += 1
                if provider.failures:
                    raise RuntimeError(provider.failures.pop(0))
                return []

            async def release(self, error: BaseException | None = None) -> None:
                return None

        return Connection()


async def test_runs_again_ddl_oracle_turned_away_while_another_sessions_ddl_held_the_table():
    provider = _Failing(
        [
            "ORA-00054: resource busy and acquire with NOWAIT specified or timeout expired",
            "ORA-06512: at line 7\nORA-04020: deadlock detected while trying to lock object JVM_NODE",
        ]
    )
    assert await SchemaExecutor(provider).run("CREATE INDEX x ON y (z)") == []  # pyright: ignore[reportArgumentType]
    assert provider.runs == 3


async def test_does_not_run_again_a_statement_oracle_refused_for_another_reason():
    provider = _Failing(["ORA-01408: such column list already indexed"])
    with pytest.raises(DrivineError, match="ORA-01408"):
        await SchemaExecutor(provider).run("CREATE INDEX x ON y (z)")  # pyright: ignore[reportArgumentType]
    assert provider.runs == 1


async def test_gives_a_filter_only_the_indexes_that_are_whole():
    ranges = [SchemaItem("Person", ("name",)), SchemaItem("Person", ("uuid",), incomplete=True)]
    constraints = [SchemaItem("Person", ("email",))]

    class Listing:
        def __init__(self, items: list[SchemaItem]) -> None:
            self.items = items

        async def list(self, *_: Any) -> list[SchemaItem]:
            return self.items

    advisor = QueryIndexAdvisor(
        lambda: Listing(ranges),  # pyright: ignore[reportArgumentType, reportReturnType]
        lambda: Listing(constraints),  # pyright: ignore[reportArgumentType, reportReturnType]
        lambda: False,
    )
    assert [it.properties for it in await advisor.indexed()] == [("name",), ("email",)]


def test_writes_an_items_properties_as_drivine4j_writes_them_byte_for_byte():
    assert catalog_properties(["lastActivityAt", "sessionId"]) == '["lastActivityAt","sessionId"]'
    assert catalog_properties(['a"b', "c\\d", "it's", "é"]) == '["a\\"b","c\\\\d","it\'s","é"]'
    # drivine4j escapes nothing else, where a JSON library would: a newline is written as it is.
    assert catalog_properties(["a\nb"]) == '["a\nb"]'


def test_finds_an_items_record_under_drivine4js_kind_or_under_any_of_the_kinds_given():
    assert (
        catalog_record("RANGE", "Person", ["it's"])
        == "kind = 'RANGE_INDEX' AND label = 'Person' AND properties = '[\"it''s\"]'"
    )
    assert (
        catalog_record("UNIQUE", "Person", ["uuid"], ["UNIQUENESS_CONSTRAINT", "UNIQUE"])
        == "kind IN ('UNIQUENESS_CONSTRAINT', 'UNIQUE') AND label = 'Person' AND properties = '[\"uuid\"]'"
    )
