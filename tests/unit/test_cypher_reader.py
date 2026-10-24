"""A Cypher statement read into the syntax tree the compiler works from: drivine4j's CypherReaderTest, ported."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from drivine import DrivineError
from drivine.cypher.ast.clause import Create, Delete, Match, Merge, Remove, Return, SetClause
from drivine.cypher.ast.expression import (
    Binary,
    BinaryOperator,
    CountStar,
    Expression,
    Function,
    HasLabels,
    Literal,
    MapLiteral,
    Parameter,
    Property,
    Slice,
    Unary,
    UnaryOperator,
    Variable,
)
from drivine.cypher.ast.node_pattern import NodePattern
from drivine.cypher.ast.pattern import Pattern
from drivine.cypher.ast.relationship_pattern import Direction, Range, RelationshipPattern
from drivine.cypher.ast.remove_item import RemoveLabels, RemoveProperty
from drivine.cypher.ast.set_item import SetAdd, SetLabels, SetProperty, SetReplace
from drivine.cypher.cypher_reader import read_cypher
from drivine.cypher.sql.parameters import cypher_json


def returned(cypher: str) -> Expression:
    last = read_cypher(cypher).clauses[-1]
    assert isinstance(last, Return)
    return last.projection.items[0].expression


def test_a_relationship_reads_its_direction_alternatives_and_range():
    match = read_cypher("MATCH (a)<-[r:KNOWS|LIKES*1..3]-(b)-[*2]->(c)-[*..4]-(d)-[*]-(e) RETURN a").clauses[0]
    assert isinstance(match, Match)
    first, second, third, fourth = match.patterns[0].relationships
    assert first == RelationshipPattern("r", ("KNOWS", "LIKES"), Direction.INCOMING, Range(1, 3), None)
    assert second.range == Range(2, 2) and second.direction is Direction.OUTGOING
    assert third.range == Range(None, 4)
    assert fourth.range == Range(None, None)


def test_a_quantified_relationship_reads_as_its_range():
    match = read_cypher("MATCH (a)-[:R]->{2,}(b)-[:R]->+(c)-[:R]->{,3}(d) RETURN a").clauses[0]
    assert isinstance(match, Match)
    assert [it.range for it in match.patterns[0].relationships] == [Range(2, None), Range(1, None), Range(0, 3)]


def test_a_chain_of_comparisons_reads_as_their_conjunction_and_precedence_is_the_grammars():
    assert returned("RETURN 1 < x <= 5") == Binary(
        BinaryOperator.AND,
        Binary(BinaryOperator.LESS_THAN, Literal(1), Variable("x")),
        Binary(BinaryOperator.LESS_THAN_OR_EQUAL, Variable("x"), Literal(5)),
    )
    assert returned("RETURN 1 + 2 * 3") == Binary(
        BinaryOperator.ADD, Literal(1), Binary(BinaryOperator.MULTIPLY, Literal(2), Literal(3))
    )


def test_strings_names_in_backticks_parameters_and_numbers_read_as_their_values():
    assert returned("RETURN 'it\\'s \"quoted\"\\n'") == Literal('it\'s "quoted"\n')
    assert returned("RETURN `my var`.`odd``key`") == Property(Variable("my var"), "odd`key")
    assert returned("RETURN $1") == Parameter("1")
    # Strict openCypher: a parameter is not named with a reserved word.
    with pytest.raises(DrivineError):
        read_cypher("RETURN $skip")
    assert returned("RETURN 0xff") == Literal(255)
    assert returned("RETURN 1.5e3") == Literal(1500.0)
    assert returned("RETURN '\\uD83D\\uDE00'") == Literal("\U0001f600")


def test_a_literal_is_of_its_kind():
    assert Literal(1) != Literal(1.0)
    assert Literal(1) != Literal(True)
    assert len({Literal(1), Literal(1.0), Literal(True)}) == 3


def test_string_list_and_null_predicates_labels_lists_and_functions_read():
    assert returned("RETURN n.name STARTS WITH $p") == Binary(
        BinaryOperator.STARTS_WITH, Property(Variable("n"), "name"), Parameter("p")
    )
    assert returned("RETURN n.x IS NOT NULL") == Unary(UnaryOperator.IS_NOT_NULL, Property(Variable("n"), "x"))
    assert returned("RETURN n:A:B") == HasLabels(Variable("n"), ("A", "B"))
    assert returned("RETURN xs[..2]") == Slice(Variable("xs"), None, Literal(2))
    assert returned("RETURN apoc.text.join(xs, ',')") == Function(
        "apoc.text.join", False, (Variable("xs"), Literal(","))
    )
    assert returned("RETURN count(DISTINCT n)") == Function("count", True, (Variable("n"),))
    assert returned("RETURN count(*)") == CountStar()


def test_a_projection_reads_its_names_distinct_order_skip_and_limit():
    last = read_cypher("MATCH (n) RETURN DISTINCT n.name AS name, n.age ORDER BY name DESC, n.age SKIP 2 LIMIT $n")
    projection = last.clauses[-1]
    assert isinstance(projection, Return)
    ret = projection.projection
    assert ret.distinct
    assert [it.name for it in ret.items] == ["name", "n.age"]
    assert [it.descending for it in ret.order_by] == [True, False]
    assert ret.skip == Literal(2) and ret.limit == Parameter("n")


def test_the_writes_read_as_their_clauses_in_order_with_or_without_a_return():
    query = read_cypher(
        """
        MATCH (a:Person {id: $id})
        CREATE (a)-[:WROTE]->(p:Paper {title: 'x'})
        MERGE (t:Topic {name: 'y'}) ON CREATE SET t.made = true ON MATCH SET t += $seen
        SET p = $props, p:Draft, a.count = a.count + 1
        REMOVE p:Draft, p.title
        WITH a, t
        DETACH DELETE t
        """.strip()
    )
    _, create, merge, set_, remove, _, delete = query.clauses
    assert create == Create(
        (
            Pattern(
                None,
                (NodePattern("a", (), None), NodePattern("p", ("Paper",), MapLiteral((("title", Literal("x")),)))),
                (RelationshipPattern(None, ("WROTE",), Direction.OUTGOING, None, None),),
            ),
        )
    )
    assert isinstance(merge, Merge)
    assert merge.on_create == (SetProperty("t", "made", Literal(True)),)
    assert merge.on_match == (SetAdd("t", Parameter("seen")),)
    assert isinstance(set_, SetClause)
    assert set_.items == (
        SetReplace("p", Parameter("props")),
        SetLabels("p", ("Draft",)),
        SetProperty("a", "count", Binary(BinaryOperator.ADD, Property(Variable("a"), "count"), Literal(1))),
    )
    assert isinstance(remove, Remove)
    assert remove.items == (RemoveLabels("p", ("Draft",)), RemoveProperty("p", "title"))
    assert delete == Delete(True, (Variable("t"),))
    with pytest.raises(DrivineError) as refused:
        read_cypher("MATCH (n) SET n.a.b = 1")
    assert (
        str(refused.value)
        == "A write to a property of anything but a variable is Cypher, and is not compiled to SQL yet."
    )


def test_a_union_reads_each_query_and_whether_it_keeps_every_row():
    query = read_cypher("RETURN 1 AS v UNION ALL RETURN 2 AS v UNION ALL RETURN 3 AS v")
    assert [it.all for it in query.unions] == [True, True]
    last = query.unions[-1].clauses[0]
    assert isinstance(last, Return) and last.projection.items[0].expression == Literal(3)


@pytest.mark.parametrize("word", ["foreach", "shortestPath", "allShortestPaths", "transactions", "rows", "row"])
def test_a_word_the_grammar_adds_is_still_a_name_of_a_variable_a_property_or_a_maps_key(word: str):
    assert returned(f"WITH 1 AS {word} RETURN {word}") == Variable(word)
    assert returned(f"RETURN n.{word}") == Property(Variable("n"), word)
    assert returned(f"RETURN {{{word}: 1}}") == MapLiteral(((word, Literal(1)),))


def test_cypher_the_compiler_does_not_take_is_refused_by_name_and_what_is_not_cypher_with_where():
    with pytest.raises(DrivineError) as refused:
        read_cypher("CALL db.labels()")
    assert str(refused.value) == "A procedure call (CALL) is Cypher, and is not compiled to SQL yet."
    # One among a query's reading clauses is refused alike.
    with pytest.raises(DrivineError) as refused:
        read_cypher("MATCH (n) CALL db.labels() YIELD label RETURN label")
    assert str(refused.value) == "A procedure call (CALL) is Cypher, and is not compiled to SQL yet."
    with pytest.raises(DrivineError, match=r"^The statement is not Cypher, at line 1, column \d+: "):
        read_cypher("MATCH (n RETURN n")


def test_a_statement_is_read_once():
    assert read_cypher("MATCH (n) RETURN n") is read_cypher("MATCH (n) RETURN n")


class Colour(Enum):
    RED = "red"


@pytest.mark.parametrize(
    ("value", "written"),
    [
        (date(2024, 3, 15), '"2024-03-15"'),
        (datetime(2024, 3, 15, 10, 30), '"2024-03-15T10:30:00"'),
        (datetime(2024, 3, 15, 10, 30, tzinfo=timezone(timedelta(hours=2))), '"2024-03-15T10:30:00+02:00"'),
        (datetime(2024, 3, 15, 10, 30, 0, 500000, tzinfo=UTC), '"2024-03-15T10:30:00.500000+00:00"'),
        (time(10, 30), '"10:30:00"'),
        (timedelta(days=1, hours=2, seconds=1.5), '"P1DT2H1.5S"'),
        (timedelta(hours=-1), '"P-1DT23H"'),
        (timedelta(0), '"PT0S"'),
        ({"n": 9007199254740993, "f": 0.1, "l": (1, "é")}, '{"n":9007199254740993,"f":0.1,"l":[1,"é"]}'),
        # An Enum as its value, and a Decimal as the number it is: whole or not.
        ({"e": Colour.RED, "s": frozenset({Colour.RED})}, '{"e":"red","s":["red"]}'),
        ([Decimal("2.00"), Decimal("2.5")], "[2,2.5]"),
    ],
)
def test_a_parameter_is_bound_as_the_json_text_of_its_value(value: Any, written: str):
    assert cypher_json(value) == written
