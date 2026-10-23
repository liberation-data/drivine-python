"""
A uniqueness violation's sample names the nodes that hold each repeated key: drivine4j's statements, from its
Neo4jSchemaGrammarTest, OracleSchemaGrammarTest, PostgresSchemaGrammarTest and SchemaCatalogTest.
"""

from typing import Annotated

from drivine import GraphProperty, NodeFragment, NodeId, SchemaCatalog, Unique, uniqueness_constraint
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.oracle.oracle_schema_grammar import oracle_schema_grammar
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.postgres.postgres_schema_grammar import postgres_schema_grammar
from drivine.schema.schema_grammar import FALKORDB_SCHEMA, MEMGRAPH_SCHEMA, NEO4J_SCHEMA
from drivine.schema.schema_manager import SchemaManager, inventory_key

PAIR = uniqueness_constraint("Person", ["email", "tenant"])


def in_order(text: str, *parts: str) -> None:
    at = 0
    for part in parts:
        found = text.find(part, at)
        assert found >= 0, f"{part!r} not after {at} in:\n{text}"
        at = found + len(part)


def test_neo4j_groups_the_labelled_nodes_that_hold_every_key_and_returns_each_repeat_with_its_count_and_nodes():
    assert NEO4J_SCHEMA.conflicts(PAIR, 5) == (
        "MATCH (n:Person)\n"
        "WHERE n.email IS NOT NULL AND n.tenant IS NOT NULL\n"
        "WITH n.email AS _k0, n.tenant AS _k1, count(*) AS _count, collect({elementId: elementId(n)}) AS _nodes\n"
        "WHERE _count > 1\n"
        "RETURN {properties: {email: _k0, tenant: _k1}, count: _count, nodes: _nodes[0..5]}\n"
        "LIMIT 5"
    )


def test_a_conflicting_node_is_named_by_the_fragments_id_too_unless_that_is_a_key():
    def by(node_id: str) -> str:
        return NEO4J_SCHEMA.conflicts(uniqueness_constraint("Person", "email", node_id_property=node_id), 3)

    assert "collect({elementId: elementId(n), uuid: n.uuid}) AS _nodes" in by("uuid")
    assert "collect({elementId: elementId(n)}) AS _nodes" in by("email")
    # A flattened property-bag key is one name, quoted as key and as property alike.
    assert "collect({elementId: elementId(n), `ref.id`: n.`ref.id`}) AS _nodes" in by("ref.id")


def test_memgraph_and_falkordb_name_a_node_by_id():
    for grammar in (MEMGRAPH_SCHEMA, FALKORDB_SCHEMA):
        assert "collect({elementId: id(n)}) AS _nodes" in grammar.conflicts(uniqueness_constraint("Person", "email"), 3)


def test_oracle_numbers_each_node_within_its_key_and_names_it_by_its_nid():
    grammar = oracle_schema_grammar(OracleGraph())
    in_order(
        grammar.conflicts(PAIR, 5),
        "SELECT JSON_OBJECT('properties' VALUE JSON_OBJECT('email' VALUE k0, 'tenant' VALUE k1), 'count' VALUE MAX(c),",
        "'nodes' VALUE JSON_ARRAYAGG(JSON_OBJECT('elementId' VALUE nid) ORDER BY nid) RETURNING JSON)",
        "FROM (SELECT n.nid, JSON_VALUE(n.props, '$.email') AS k0, JSON_VALUE(n.props, '$.tenant') AS k1, "
        "COUNT(*) OVER (PARTITION BY JSON_VALUE(n.props, '$.email'), JSON_VALUE(n.props, '$.tenant')) AS c, "
        "ROW_NUMBER() OVER (PARTITION BY JSON_VALUE(n.props, '$.email'), JSON_VALUE(n.props, '$.tenant') ORDER BY "
        "n.nid) AS r",
        "WHERE JSON_EXISTS(n.labels, '$[*]?(@ == \"Person\")') AND JSON_VALUE(n.props, '$.email') IS NOT NULL AND "
        "JSON_VALUE(n.props, '$.tenant') IS NOT NULL)",
        "WHERE c > 1 AND r <= 5",
        "GROUP BY k0, k1",
        "FETCH FIRST 5 ROWS ONLY",
    )


def test_oracle_names_a_conflicting_node_by_the_fragments_id_too_unless_that_is_a_key():
    grammar = oracle_schema_grammar(OracleGraph())
    in_order(
        grammar.conflicts(uniqueness_constraint("Person", "email", node_id_property="uuid"), 3),
        "'nodes' VALUE JSON_ARRAYAGG(JSON_OBJECT('elementId' VALUE nid, 'uuid' VALUE i) ORDER BY nid)",
        "FROM (SELECT n.nid, JSON_VALUE(n.props, '$.email') AS k0, JSON_VALUE(n.props, '$.uuid') AS i, COUNT(*) OVER",
    )
    in_order(
        grammar.conflicts(uniqueness_constraint("Person", "email", node_id_property="email"), 3),
        "'nodes' VALUE JSON_ARRAYAGG(JSON_OBJECT('elementId' VALUE nid) ORDER BY nid)",
        "FROM (SELECT n.nid, JSON_VALUE(n.props, '$.email') AS k0, COUNT(*) OVER",
    )


def test_postgres_slices_the_nodes_each_key_aggregates_and_names_each_by_its_nid():
    grammar = postgres_schema_grammar(PostgresGraph("g"))
    assert grammar.conflicts(PAIR, 5) == (
        "SELECT jsonb_build_object('properties', jsonb_build_object('email', (n.props ->> 'email'), 'tenant', "
        "(n.props ->> 'tenant')), 'count', COUNT(*),\n"
        "                          'nodes', to_jsonb((array_agg(jsonb_build_object('elementId', n.nid) ORDER BY "
        "n.nid))[1:5]))\n"
        "FROM g_node n\n"
        "WHERE n.labels @> '[\"Person\"]' AND (n.props ->> 'email') IS NOT NULL AND (n.props ->> 'tenant') "
        "IS NOT NULL\n"
        "GROUP BY (n.props ->> 'email'), (n.props ->> 'tenant')\n"
        "HAVING COUNT(*) > 1\n"
        "LIMIT 5"
    )


def test_postgres_quotes_the_fragments_id_and_leaves_it_out_when_it_is_a_key():
    grammar = postgres_schema_grammar(PostgresGraph("g"))
    by_quoted = grammar.conflicts(uniqueness_constraint("Person", "email", node_id_property="it's"), 3)
    assert "array_agg(jsonb_build_object('elementId', n.nid, 'it''s', (n.props ->> 'it''s')) ORDER BY n.nid))[1:3]" in (
        by_quoted
    )
    by_key = grammar.conflicts(uniqueness_constraint("Person", "email", node_id_property="email"), 3)
    assert "array_agg(jsonb_build_object('elementId', n.nid) ORDER BY n.nid))[1:3]" in by_key


class Member(NodeFragment, labels=["Member"]):
    uuid: Annotated[str, NodeId()]
    email: Annotated[str, Unique()]


class Anonymous(NodeFragment, labels=["Anonymous"]):
    email: Annotated[str, Unique()]


class Renamed(NodeFragment, labels=["Renamed"]):
    ref: Annotated[str, NodeId(), GraphProperty("member_ref")]
    email: Annotated[str, Unique()]


def test_the_fragment_scan_names_a_violations_nodes_by_the_stored_node_id():
    [member] = SchemaCatalog.from_fragments(Member).items
    assert member == uniqueness_constraint("Member", "email", node_id_property="uuid")
    [anonymous] = SchemaCatalog.from_fragments(Anonymous).items
    assert anonymous == uniqueness_constraint("Anonymous", "email")
    [renamed] = SchemaCatalog.from_fragments(Renamed).items
    assert renamed.kind == "UNIQUE" and renamed.node_id_property == "member_ref"


def test_the_node_id_makes_nothing_so_a_fragments_declaration_and_a_hand_written_one_are_one_keeping_it():
    scanned = uniqueness_constraint("Member", "email", node_id_property="uuid")
    by_hand = uniqueness_constraint("Member", "email")
    assert inventory_key(scanned) == inventory_key(by_hand)
    for first, second in ((scanned, by_hand), (by_hand, scanned)):
        assert SchemaManager._reconciled("default", first, second) == scanned  # pyright: ignore[reportPrivateUsage]
