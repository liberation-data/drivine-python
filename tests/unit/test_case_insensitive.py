"""
CaseInsensitive: the lowercased copy a save keeps of a property, the items over it, the record that every node has it,
and the lookups that read it. The layout and statements are drivine4j's (liberation-data/drivine4j#149), as its
CaseInsensitiveStatementsTest pins them, so a store written by one library is read by the others.
"""

from collections.abc import Mapping
from typing import Annotated, Any

import pytest

from drivine import (
    CaseInsensitive,
    DrivineError,
    GraphProperty,
    JsonPacked,
    ModelRegistry,
    NodeFragment,
    NodeId,
    PropertyBag,
    RangeIndex,
    SchemaCatalog,
    Transient,
    Unique,
    any_of,
    lower_copies,
    not_,
    range_index,
    refs,
    uniqueness_constraint,
)
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import NEO4J_GRAMMAR
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.connection.database_type import DatabaseType
from drivine.dsl.compare_test import CompareTest
from drivine.dsl.composite_condition import CompositeCondition
from drivine.dsl.lower_copy_lookups import lowered_where
from drivine.dsl.path_condition import PathCondition
from drivine.dsl.query import Query
from drivine.dsl.ref import STORED
from drivine.manager.save_options import SaveOptions
from drivine.schema.constraint_manager import ConstraintManager
from drivine.schema.index_manager import IndexManager
from drivine.schema.lower_copy_catalog import LowerCopyCatalog
from drivine.schema.lower_copy_statements import (
    CYPHER_LOWER_COPIES,
    LowerCopyStatements,
    Recording,
    catalog_entries,
    lowered_entry,
)
from drivine.schema.schema_grammar import FALKORDB_SCHEMA, MEMGRAPH_SCHEMA, NEO4J_SCHEMA, schema_grammar_for
from drivine.schema.schema_manager import inventory_key
from drivine.schema.schema_specs import item_name, lowercased, made_as
from tests.models import Account, AccountFriends


class Member(NodeFragment, labels=["Member"]):
    uuid: Annotated[str, NodeId()]
    email: Annotated[str, CaseInsensitive(), Unique(ignore_case=True)]
    handle: Annotated[str | None, CaseInsensitive(), RangeIndex(ignore_case=True)] = None
    name: Annotated[str | None, RangeIndex()] = None


class TestTheCopy:
    def test_is_kept_of_each_case_insensitive_field_under_its_stored_name_in_the_order_of_the_fields(self):
        assert ModelRegistry().fragment(Account).lower_copied == ["display_name", "email"]
        assert ModelRegistry(property_names="camel").fragment(Member).lower_copied == ["email", "handle"]

    def test_is_set_by_the_engine_from_the_property(self):
        assert lower_copies.of("email") == "__drivine.lower.email"
        assert lower_copies.set_clause("n", "email") == "n.`__drivine.lower.email` = toLower(n.email)"
        assert lower_copies.set_clause("n", "display_name") == (
            "n.`__drivine.lower.display_name` = toLower(n.display_name)"
        )
        assert lower_copies.clauses("n", []) == []

    @pytest.mark.parametrize(
        "annotation, refused",
        [
            (Annotated[int, CaseInsensitive()], "not text"),
            (Annotated[list[str], CaseInsensitive()], "not text"),
            (Annotated[dict[str, Any], CaseInsensitive(), PropertyBag()], "a PropertyBag field"),
            (Annotated[str, CaseInsensitive(), JsonPacked()], "a JsonPacked field"),
            (Annotated[str, CaseInsensitive(), Transient()], "a Transient field"),
        ],
    )
    def test_is_refused_on_a_field_that_isnt_text_stored_as_its_own_property(self, annotation: Any, refused: str):
        fragment = type(
            "Odd",
            (NodeFragment,),
            {"__annotations__": {"uuid": Annotated[str, NodeId()], "field": annotation}, "field": "x"},
            labels=["Odd"],
        )
        with pytest.raises(DrivineError, match=refused):
            ModelRegistry().fragment(fragment)

    def test_follows_a_renamed_field(self):
        class Renamed(NodeFragment, labels=["Renamed"]):
            uuid: Annotated[str, NodeId()]
            mail: Annotated[str, CaseInsensitive(), GraphProperty("e_mail")]

        assert ModelRegistry().fragment(Renamed).lower_copied == ["e_mail"]


class TestWrites:
    """Every write path keeps the copy, in a SET of its own, after the properties are written."""

    registry = ModelRegistry()
    copies = (
        "SET n.`__drivine.lower.display_name` = toLower(n.display_name), n.`__drivine.lower.email` = toLower(n.email)"
    )

    def test_a_save(self):
        statement = CypherBackend(NEO4J_GRAMMAR).save(
            self.registry.fragment(Account), Account(uuid="a1", email="A@X.com"), None, False, self.registry
        )
        assert f"SET n += $root.props\n{self.copies}" in statement.text

    def test_a_views_root_and_related_nodes(self):
        statement = CypherBackend(NEO4J_GRAMMAR).save(
            self.registry.view(AccountFriends),
            AccountFriends(account=Account(uuid="a1"), friends=[Account(uuid="a2")]),
            None,
            False,
            self.registry,
        )
        assert self.copies in statement.text
        assert "SET t1.`__drivine.lower.display_name` = toLower(t1.display_name)" in statement.text

    def test_a_batch(self):
        statement = CypherBackend(NEO4J_GRAMMAR).fragment_batch(
            [FragmentGroup(self.registry.fragment(Account), [Account(uuid="a1")])], SaveOptions()
        )
        assert f"SET n += row.props {self.copies})" in statement.text

    def test_a_patch_only_on_a_node_it_changed(self):
        text = CypherBackend(NEO4J_GRAMMAR).patch_one(self.registry.fragment(Account)).text
        assert (
            "FOREACH (x IN CASE WHEN changed THEN [n] ELSE [] END | SET x.`__drivine.lower.display_name` = "
            "toLower(x.display_name), x.`__drivine.lower.email` = toLower(x.email))"
        ) in text

    def test_none_for_a_fragment_without_case_insensitive_fields(self):
        from tests.models import Person

        text = CypherBackend(NEO4J_GRAMMAR).patch_one(self.registry.fragment(Person)).text
        assert "__drivine.lower" not in text

    def test_on_oracle_a_block_lowers_the_written_node(self):
        statement = OracleBackend(OracleGraph()).save(
            self.registry.fragment(Account), Account(uuid="a1", email="A@X.com"), None, False, self.registry
        )
        assert "lower_node(v_root, $x_" in statement.text
        assert "PROCEDURE lower_node(p NUMBER, p_keys VARCHAR2) IS" in statement.text
        assert '["display_name","email"]' in statement.bindings.values()

    def test_on_oracle_a_block_without_copies_declares_no_lower_node(self):
        from tests.models import Person

        text = OracleBackend(OracleGraph()).patch_one(self.registry.fragment(Person)).text
        assert "lower_node" not in text

    def test_on_postgres_the_node_and_patch_steps_carry_lower(self):
        backend = PostgresBackend(PostgresGraph())
        saved = backend.save(self.registry.fragment(Account), Account(uuid="a1"), None, False, self.registry)
        assert '"lower":["display_name","email"]' in saved.spec({}).parameters["_plan"]
        patched = backend.patch_one(self.registry.fragment(Account))
        plan = patched.spec({"id": "a1", "set": {"email": "B@X.com"}, "vectors": {}}).parameters["_plan"]
        assert '"lower":["display_name","email"]' in plan


class TestItemsOverTheCopy:
    def test_are_named_and_inventoried_as_drivine4j_makes_them(self):
        range_ = range_index("Account", "email", ignore_case=True)
        unique = uniqueness_constraint("Account", "email", ignore_case=True)
        assert item_name(range_) == "Account_email_ci_range"
        assert item_name(unique) == "Account_email_ci_unique"
        assert inventory_key(range_) == "RANGE_INDEX:Account:__drivine.lower.email:Account_email_ci_range"
        assert inventory_key(unique) == "UNIQUENESS_CONSTRAINT:Account:__drivine.lower.email:Account_email_ci_unique"
        assert made_as(range_).properties == ("__drivine.lower.email",)
        assert lowercased(range_) == ("email",)
        assert lowercased(range_index("Account", "email")) == ()

    def test_are_apart_from_items_over_the_property_itself(self):
        assert inventory_key(range_index("Account", "email")) == "RANGE_INDEX:Account:email:Account_email_range"
        assert made_as(range_index("Account", "email")) == range_index("Account", "email")

    def test_are_made_over_the_copy_on_each_engine(self):
        range_ = made_as(range_index("Account", "email", ignore_case=True))
        unique = made_as(uniqueness_constraint("Account", "email", ignore_case=True))
        assert NEO4J_SCHEMA.create_range(range_) == [
            "CREATE INDEX Account_email_ci_range IF NOT EXISTS FOR (n:Account) ON (n.`__drivine.lower.email`)"
        ]
        assert NEO4J_SCHEMA.create_unique(unique) == [
            "CREATE CONSTRAINT Account_email_ci_unique IF NOT EXISTS FOR (n:Account) "
            "REQUIRE n.`__drivine.lower.email` IS UNIQUE"
        ]
        assert MEMGRAPH_SCHEMA.create_range(range_) == ["CREATE INDEX ON :Account(`__drivine.lower.email`)"]
        assert FALKORDB_SCHEMA.create_range(range_) == ["CREATE INDEX FOR (n:Account) ON (n.`__drivine.lower.email`)"]

    def test_falkordb_refuses_a_constraint_over_the_copy(self):
        with pytest.raises(DrivineError, match="FalkorDB/FalkorDB/issues/551"):
            FALKORDB_SCHEMA.require_constrainable(made_as(uniqueness_constraint("Account", "email", ignore_case=True)))
        FALKORDB_SCHEMA.require_constrainable(uniqueness_constraint("Account", "email"))

    def test_are_declared_on_fields(self):
        catalog = SchemaCatalog.from_fragments(Member)
        assert catalog.items == (
            uniqueness_constraint("Member", "email", node_id_property="uuid", ignore_case=True),
            range_index("Member", "handle", ignore_case=True),
            range_index("Member", "name"),
        )

    def test_are_refused_over_a_field_that_keeps_no_copy(self):
        class Uncopied(NodeFragment, labels=["Uncopied"]):
            uuid: Annotated[str, NodeId()]
            email: Annotated[str, RangeIndex(ignore_case=True)]

        with pytest.raises(DrivineError, match="Add CaseInsensitive"):
            SchemaCatalog.from_fragments(Uncopied)


class TestTheRecord:
    def test_on_the_graph_engines_is_an_entry_on_the_store_node(self):
        assert lowered_entry("Account", "email") == '["Account","email"]'
        assert lowered_entry('A"c', "e\\m") == '["A\\"c","e\\\\m"]'
        recording = CYPHER_LOWER_COPIES.record("Account", "email")
        assert recording.statement == (
            "MATCH (s:DrivineStore {key: 'singleton'})\n"
            "SET s.`__drivine.lock` = true REMOVE s.`__drivine.lock`\n"
            "SET s.lowered = [e IN coalesce(s.lowered, []) WHERE e <> $entry] + $entry"
        )
        assert recording.bindings == {"entry": '["Account","email"]'}
        assert CYPHER_LOWER_COPIES.read == (
            "MATCH (s:DrivineStore) UNWIND coalesce(s.lowered, []) AS e RETURN DISTINCT e"
        )
        assert CYPHER_LOWER_COPIES.entries(['["Account","email"]', {"e": '["A","b"]'}, "nonsense", '["x"]']) == [
            ("Account", "email"),
            ("A", "b"),
        ]

    def test_on_oracle_and_postgres_is_a_catalog_row(self):
        for type_ in (DatabaseType.ORACLE, DatabaseType.POSTGRES):
            statements = schema_grammar_for(type_).lower_copies
            assert not statements.on_store_node
            assert "'LOWER_COPY'" in statements.record("Account", "email").statement
            assert "'[\"email\"]'" in statements.record("Account", "email").statement
            assert "NULL, NULL, '{}')" in statements.record("Account", "email").statement
        rows = [{"label": "Account", "properties": '["email"]'}, {"label": "A", "properties": "x"}]
        assert catalog_entries(rows) == [("Account", "email")]


class TestLookups:
    registry = ModelRegistry()

    def test_read_the_root_s_copy_once_it_is_filled_in(self):
        a = refs(Account)
        where = lowered_where(
            [
                a.email.eq_ignore_case("Ada@X.com"),
                any_of(a.display_name.contains_ignore_case("AD"), a.name.eq_ignore_case("Ada")),
                not_(a.email.eq_ignore_case("")),
            ],
            self.registry.fragment(Account),
            {"email", "display_name"},
        )
        assert isinstance(where, list)
        assert where[0] == PathCondition((f"{STORED}__drivine.lower.email",), CompareTest("EQ_LOWERED", "Ada@X.com"))
        either = where[1]
        assert isinstance(either, CompositeCondition)
        assert either.conditions[0] == PathCondition(
            (f"{STORED}__drivine.lower.display_name",), CompareTest("CONTAINS_LOWERED", "AD")
        )
        # Not a field without a copy, nor an empty value, which Oracle keeps no copy of.
        assert either.conditions[1] == a.name.eq_ignore_case("Ada")
        assert where[2] == not_(a.email.eq_ignore_case(""))

    def test_leave_a_related_node_and_an_unfilled_copy_alone(self):
        f = refs(AccountFriends)
        view = self.registry.view(AccountFriends)
        asked = [f.account.email.eq_ignore_case("A"), f.friends.email.eq_ignore_case("B")]
        where = lowered_where(asked, view, {"email"})
        assert isinstance(where, list)
        assert where[0] == PathCondition(("account", f"{STORED}__drivine.lower.email"), CompareTest("EQ_LOWERED", "A"))
        assert where[1] == f.friends.email.eq_ignore_case("B")
        unfilled = [refs(Account).email.eq_ignore_case("A")]
        assert lowered_where(unfilled, self.registry.fragment(Account), set()) == unfilled

    @pytest.mark.parametrize(
        "backend, written",
        [
            (CypherBackend(NEO4J_GRAMMAR), "n.`__drivine.lower.email` = toLower($_q0)"),
            (OracleBackend(OracleGraph()), "JSON_VALUE(n.props, '$.\"__drivine.lower.email\"') = LOWER($_q0)"),
            (PostgresBackend(PostgresGraph()), "(n.props ->> '__drivine.lower.email') = lower(CAST($_q0 AS text))"),
        ],
    )
    def test_compare_with_the_value_as_the_store_lowercases_it(self, backend: Any, written: str):
        account = self.registry.fragment(Account)
        where = lowered_where(refs(Account).email.eq_ignore_case("Ada@X.com"), account, {"email"})
        rendered = backend.render_query(Query(where=where), account, lambda it: self.registry.fragment(it).labels)
        assert written in rendered.where
        assert "Ada@X.com" in rendered.bindings.values()


class _Store:
    """A store of records, as the statements read and write it, that counts what fills in."""

    def __init__(self, unfilled: int = 0) -> None:
        self.records: set[tuple[str, str]] = set()
        self.unfilled = unfilled
        self.statements: list[str] = []

    async def run(self, statement: str, bindings: Mapping[str, Any] | None) -> list[Any]:
        self.statements.append(statement)
        if statement.startswith("FILL"):
            written = min(self.unfilled, 3)
            self.unfilled -= written
            return [written]
        if statement.startswith("RECORD"):
            self.records.add(tuple(statement.split()[1:]))  # pyright: ignore[reportArgumentType]
            return []
        return [list(it) for it in self.records]


def _statements() -> LowerCopyStatements:
    return LowerCopyStatements(
        read="READ",
        fill=lambda label, property, batch: f"FILL {label} {property} {batch}",
        record=lambda label, property: Recording(f"RECORD {label} {property}"),
        entries=lambda rows: [(it[0], it[1]) for it in rows],
        on_store_node=True,
    )


class TestTheCatalog:
    async def test_fills_in_batch_by_batch_and_records_it(self):
        store = _Store(unfilled=7)
        resolved: list[bool] = []

        async def resolve() -> None:
            resolved.append(True)

        catalog = LowerCopyCatalog(store.run, _statements(), resolve, batch=3)
        await catalog.fill_in("Account", ["email"], False)
        assert [it for it in store.statements if it.startswith("FILL")] == ["FILL Account email 3"] * 3
        assert store.records == {("Account", "email")}
        assert resolved == [True]
        assert await catalog.filled(["Account"], ["email", "name"]) == {"email"}

    async def test_leaves_a_recorded_copy_unless_again(self):
        store = _Store()
        store.records.add(("Account", "email"))

        async def resolve() -> None:
            return None

        catalog = LowerCopyCatalog(store.run, _statements(), resolve)
        await catalog.fill_in("Account", ["email"], False)
        assert not any(it.startswith("FILL") for it in store.statements)
        await catalog.fill_in("Account", ["email"], True)
        assert any(it.startswith("FILL") for it in store.statements)

    async def test_reads_what_another_recorded_again_once_it_is_old(self):
        store = _Store()
        now = [0.0]

        async def resolve() -> None:
            return None

        catalog = LowerCopyCatalog(store.run, _statements(), resolve, max_age=60, clock=lambda: now[0])
        assert not await catalog.filled_in("Account", "email")
        store.records.add(("Account", "email"))
        now[0] = 59
        assert not await catalog.filled_in("Account", "email")
        now[0] = 60
        assert await catalog.filled_in("Account", "email")

    async def test_counts_a_store_whose_record_cant_be_read_as_unfilled(self):
        async def run(statement: str, bindings: Mapping[str, Any] | None) -> list[Any]:
            raise DrivineError("no catalog")

        async def resolve() -> None:
            return None

        assert not await LowerCopyCatalog(run, _statements(), resolve).filled_in("Account", "email")

    async def test_is_needed_to_ensure_an_item_that_ignores_case(self):
        class Empty:
            async def run(self, statement: Any, bindings: Any = None) -> list[Any]:
                return []

        manager = IndexManager(Empty(), NEO4J_SCHEMA)  # pyright: ignore[reportArgumentType]
        with pytest.raises(DrivineError, match="no store to fill the lowercased copies in through"):
            await manager.ensure(range_index("Account", "email", ignore_case=True))

    async def test_falkordb_refuses_before_anything_is_filled_in(self):
        store = _Store()

        async def resolve() -> None:
            return None

        manager = ConstraintManager(None, FALKORDB_SCHEMA, copies=LowerCopyCatalog(store.run, _statements(), resolve))  # pyright: ignore[reportArgumentType]
        with pytest.raises(DrivineError, match="551"):
            await manager.ensure(uniqueness_constraint("Account", "email", ignore_case=True))
        assert store.statements == []
