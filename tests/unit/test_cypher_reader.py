"""A Cypher statement read into the syntax tree the compiler works from: drivine4j's CypherReaderTest, ported."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from drivine import DrivineError
from drivine.cypher.ast.clause import (
    Call,
    Create,
    Delete,
    Foreach,
    Match,
    Merge,
    Remove,
    Return,
    SetClause,
    Unwind,
    With,
)
from drivine.cypher.ast.expression import (
    Binary,
    BinaryOperator,
    Case,
    CountStar,
    Entry,
    Expression,
    Function,
    HasLabels,
    ListComprehension,
    Literal,
    MapLiteral,
    MapProjection,
    Parameter,
    PatternComprehension,
    Property,
    Quantifier,
    QuantifierKind,
    Reduce,
    Selected,
    Slice,
    Subquery,
    SubqueryKind,
    Unary,
    UnaryOperator,
    Variable,
    VariableElement,
)
from drivine.cypher.ast.node_pattern import NodePattern
from drivine.cypher.ast.pattern import Pattern, Shortest
from drivine.cypher.ast.projection import Projection, ProjectionItem, SortItem
from drivine.cypher.ast.relationship_pattern import Direction, Group, Range, RelationshipPattern
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


def test_a_quantified_path_pattern_reads_as_a_relationship_of_its_group():
    match = read_cypher("MATCH (a) ((x:P)-[r:R]->(y) WHERE x.v < y.v){1,3} (b) RETURN a").clauses[0]
    assert isinstance(match, Match)
    where = Binary(BinaryOperator.LESS_THAN, Property(Variable("x"), "v"), Property(Variable("y"), "v"))
    group = Group(
        Pattern(
            None,
            (NodePattern("x", ("P",), None), NodePattern("y", (), None)),
            (RelationshipPattern("r", ("R",), Direction.OUTGOING, None, None),),
        ),
        where,
    )
    assert match.patterns == (
        Pattern(
            None,
            (NodePattern("a", (), None), NodePattern("b", (), None)),
            (RelationshipPattern(None, (), Direction.OUTGOING, Range(1, 3), None, group),),
        ),
    )


def test_a_pattern_in_parentheses_with_no_quantifier_is_the_pattern_and_a_shortest_path_is_unchanged():
    def patterns(cypher: str) -> tuple[Pattern, ...]:
        match = read_cypher(cypher).clauses[0]
        assert isinstance(match, Match)
        return match.patterns

    assert patterns("MATCH (a)-[:R]->(b) RETURN a") == patterns("MATCH ((a)-[:R]->(b)) RETURN a")
    (shortest,) = patterns("MATCH p = shortestPath((a)-[:R*]->(b)) RETURN p")
    assert shortest.shortest is Shortest.ONE
    assert shortest.relationships == (RelationshipPattern(None, ("R",), Direction.OUTGOING, Range(None, None), None),)


@pytest.mark.parametrize(
    ("empty", "without"),
    [
        ("MATCH (a {}) RETURN a", "MATCH (a) RETURN a"),
        ("MATCH (a:X {})-[r:R {}]->(b {}) RETURN r", "MATCH (a:X)-[r:R]->(b) RETURN r"),
        ("MATCH (a)-[:R*1..2 {}]->(b) RETURN b", "MATCH (a)-[:R*1..2]->(b) RETURN b"),
        ("MATCH (a) WHERE EXISTS { (a)-[:R {}]->({}) } RETURN a", "MATCH (a) WHERE EXISTS { (a)-[:R]->() } RETURN a"),
        ("MERGE (a:X {}) CREATE (a)-[:R {}]->(:Y {})", "MERGE (a:X) CREATE (a)-[:R]->(:Y)"),
    ],
)
def test_an_empty_map_of_properties_in_a_pattern_reads_as_none_as_every_node_and_relationship_meets_it(
    empty: str, without: str
):
    # Read as a map, it compiled to no test at all, an empty WHERE or a dangling AND on the SQL stores (drivine4j#583).
    assert read_cypher(empty) == read_cypher(without)
    assert hash(read_cypher(empty)) == hash(read_cypher(without))


def test_a_map_of_properties_with_a_key_and_a_parameters_map_are_still_read_as_they_are():
    match = read_cypher("MATCH (a {k: 1}), (b $p) RETURN a").clauses[0]
    assert isinstance(match, Match)
    first, second = (it.nodes[0].properties for it in match.patterns)
    assert isinstance(first, MapLiteral) and isinstance(second, Parameter), (first, second)


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
    # As in Neo4j, and not strict openCypher: a parameter may be named with a reserved word.
    assert returned("RETURN $skip") == Parameter("skip")
    assert returned("RETURN 0xff") == Literal(255)
    assert returned("RETURN 1.5e3") == Literal(1500.0)
    assert returned("RETURN '\\uD83D\\uDE00'") == Literal("\U0001f600")
    # The least whole number is its digits negated, which alone are past the greatest.
    assert returned("RETURN -9223372036854775808") == Literal(-9223372036854775808)
    assert returned("RETURN -0x8000000000000000") == Literal(-9223372036854775808)
    assert returned("RETURN -1") == Unary(UnaryOperator.NEGATE, Literal(1))
    with pytest.raises(DrivineError) as refused:
        read_cypher("RETURN 9223372036854775808")
    assert str(refused.value) == (
        "9223372036854775808 is past the range of a whole number, from -9223372036854775808 to 9223372036854775807."
    )
    with pytest.raises(DrivineError) as refused:
        read_cypher("RETURN -9223372036854775809")
    assert str(refused.value) == (
        "-9223372036854775809 is past the range of a whole number, from -9223372036854775808 to 9223372036854775807."
    )


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
    # As Neo4j names them (#311): an unnamed map projection after its variable, an alias before it, any other item as
    # written.
    maps = read_cypher("MATCH (p) RETURN p {.name}, p {.name, .age} AS q, p {.name}.name").clauses[-1]
    assert isinstance(maps, Return)
    assert [it.name for it in maps.projection.items] == ["p", "q", "p {.name}.name"]


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


@pytest.mark.parametrize(
    "word", ["limit", "skip", "order", "match", "count", "end", "call", "yield", "Return", "null", "foreach"]
)
def test_a_parameter_is_named_by_any_word_a_keyword_among_them_in_a_page_a_condition_a_map_an_unwind_and_a_set(
    word: str,
):
    parameter = Parameter(word)
    assert returned(f"RETURN ${word}") == parameter
    match, unwind, ret = read_cypher(
        f"MATCH (n {{k: ${word}}}) WHERE n.x = ${word} UNWIND ${word} AS i RETURN i SKIP ${word} LIMIT ${word}"
    ).clauses
    assert isinstance(match, Match) and isinstance(unwind, Unwind) and isinstance(ret, Return)
    properties = match.patterns[0].nodes[0].properties
    assert isinstance(properties, MapLiteral) and properties.entries[0][1] == parameter
    assert isinstance(match.where, Binary) and match.where.right == parameter
    assert unwind.list == parameter
    assert [ret.projection.skip, ret.projection.limit] == [parameter, parameter]
    set_ = read_cypher(f"MATCH (n) SET n.x = ${word}, n += ${word}").clauses[-1]
    assert isinstance(set_, SetClause)
    assert list(set_.items) == [SetProperty("n", "x", parameter), SetAdd("n", parameter)]


def test_a_call_of_db_labels_or_db_relationship_types_reads_as_the_query_that_gives_its_rows():
    labels = read_cypher("CALL db.labels()")
    unwound = labels.clauses[1]
    assert isinstance(unwound, Unwind)
    assert unwound.list == Function("labels", False, (Variable("#procedure"),))
    last = labels.clauses[-1]
    assert isinstance(last, Return)
    assert last.projection.distinct
    assert [it.name for it in last.projection.items] == ["label"]
    # Within a query, a CALL { … } of its own, its WHERE a WITH's in it, and what it yields named as the YIELD names it.
    call = read_cypher(
        "MATCH (m) CALL db.relationshipTypes() YIELD relationshipType AS t WHERE t <> m.x RETURN t"
    ).clauses[1]
    assert isinstance(call, Call)
    _, with_, ret = call.query.clauses
    assert isinstance(with_, With) and isinstance(ret, Return)
    assert with_.projection.items[0].expression == Function("type", False, (Variable("#procedure"),))
    assert [it.name for it in with_.projection.items] == ["t"]
    assert isinstance(with_.where, Binary) and with_.where.left == Variable("t")
    assert [it.name for it in ret.projection.items] == ["t"]


def test_cypher_the_compiler_does_not_take_is_refused_by_name_and_what_is_not_cypher_with_where():
    with pytest.raises(DrivineError) as refused:
        read_cypher("CALL db.indexes()")
    assert str(refused.value) == "A procedure call (CALL) is Cypher, and is not compiled to SQL yet."
    # One among a query's reading clauses is refused alike.
    with pytest.raises(DrivineError) as refused:
        read_cypher("MATCH (n) CALL db.indexes() YIELD label RETURN label")
    assert str(refused.value) == "A procedure call (CALL) is Cypher, and is not compiled to SQL yet."
    with pytest.raises(DrivineError, match=r"^The statement is not Cypher, at line 1, column \d+: "):
        read_cypher("MATCH (n RETURN n")


def test_a_reading_clause_straight_after_a_write_reads_in_the_order_written():
    clauses = read_cypher("CREATE (n:Thing) UNWIND [1, 2] AS x MATCH (m:Thing) RETURN count(m)").clauses
    assert [type(it) for it in clauses] == [Create, Unwind, Match, Return]
    clauses = read_cypher("MATCH (n) SET n.a = 1 MATCH (m) DELETE m").clauses
    assert [type(it) for it in clauses] == [Match, SetClause, Match, Delete]
    # And in a part that ends in a WITH.
    clauses = read_cypher("CREATE (n:Thing) MATCH (m:Thing) WITH count(m) AS c RETURN c").clauses
    assert [type(it) for it in clauses] == [Create, Match, With, Return]
    # As in Neo4j, a query that returns nothing ends in a write.
    with pytest.raises(DrivineError):
        read_cypher("CREATE (n) MATCH (m)")


def test_a_statement_is_read_once():
    assert read_cypher("MATCH (n) RETURN n") is read_cypher("MATCH (n) RETURN n")


@pytest.mark.parametrize(
    ("cypher", "at"),
    [
        ("MATCH (n RETURN n", "line 1, column 10"),
        ("RETURN CASE WHEN 1 THEN 2", "line 1, column 26"),
        ("RETURN CASE WHEN true THEN end", "line 1, column 31"),
        ("MATCH (n) RETURN n ORDER", "line 1, column 25"),
        ("MATCH (n)\nWITH n\nRETURN n ORDER BY", "line 3, column 18"),
    ],
)
def test_what_is_still_not_cypher_now_a_keyword_may_name_a_variable_is_refused_where_it_stops_being_cypher(
    cypher: str, at: str
):
    with pytest.raises(DrivineError) as refused:
        read_cypher(cypher)
    assert str(refused.value).startswith(f"The statement is not Cypher, at {at}: "), str(refused.value)


def test_a_keyword_names_a_variable_as_neo4j_takes_it_and_a_case_still_ends_at_its_end():
    # APOC's export names a relationship's nodes start and end (drivine4j#635).
    apoc = read_cypher(
        "UNWIND [{start: {_id: 1}, end: {_id: 2}}] AS row MATCH (start:L {id: row.start._id}) "
        "MATCH (end:L {id: row.end._id}) CREATE (start)-[r:T]->(end)"
    )
    _, _, end, create = apoc.clauses
    assert isinstance(end, Match) and isinstance(create, Create)
    assert [it.variable for it in end.patterns[0].nodes] == ["end"]
    assert [it.variable for it in create.patterns[0].nodes] == ["start", "end"]
    assert returned("WITH 1 AS when, 2 AS end, 3 AS else RETURN CASE WHEN when THEN end ELSE else END") == Case(
        None, ((Variable("when"), Variable("end")),), Variable("else")
    )
    for word in [
        "match", "MATCH", "order", "return", "case", "distinct", "not", "in", "is", "as", "all", "call", "yield",
        "skip", "limit", "union", "exists",
    ]:  # fmt: skip
        assert returned(f"UNWIND [1] AS {word} RETURN {word}") == Variable(word), word
    assert returned("UNWIND [1] AS desc RETURN desc ORDER BY desc DESC") == Variable("desc")
    # As written, so End is another variable than end, and between backticks the same.
    for cypher, variable in [("MATCH (End) RETURN end", "End"), ("MATCH (`end`) RETURN end", "end")]:
        match = read_cypher(cypher).clauses[0]
        assert isinstance(match, Match)
        assert [it.variable for it in match.patterns[0].nodes] == [variable]
    # A value stays one.
    assert returned("RETURN null") == Literal(None)
    assert returned("RETURN true") == Literal(True)
    assert returned("WITH true AS x RETURN NOT x") == Unary(UnaryOperator.NOT, Variable("x"))


def test_a_keyword_names_a_variable_wherever_a_variable_stands():
    v = Variable
    _, foreach = read_cypher("UNWIND [1] AS end FOREACH (end IN [1] | CREATE (:A {v: end}))").clauses
    assert isinstance(foreach, Foreach) and foreach.variable == "end"
    call = read_cypher("WITH 1 AS end, 2 AS case CALL (end, case) { RETURN end + case AS e } RETURN e").clauses[1]
    assert isinstance(call, Call) and call.imports == ("end", "case")
    _, set_, remove = read_cypher(
        "MATCH (end) SET end.x = 1, end:L, end += {a: 1}, end = {} REMOVE end.x, end:L"
    ).clauses
    assert isinstance(set_, SetClause) and isinstance(remove, Remove)
    assert set_.items == (
        SetProperty("end", "x", Literal(1)),
        SetLabels("end", ("L",)),
        SetAdd("end", MapLiteral((("a", Literal(1)),))),
        SetReplace("end", MapLiteral(())),
    )
    assert remove.items == (RemoveProperty("end", "x"), RemoveLabels("end", ("L",)))
    assert returned("CALL db.labels() YIELD label AS end RETURN end") == v("end")
    path = read_cypher("MATCH end = (a)-[case]->(b) RETURN end").clauses[0]
    assert isinstance(path, Match)
    assert path.patterns[0].variable == "end"
    assert [it.variable for it in path.patterns[0].relationships] == ["case"]
    assert returned("WITH [1] AS l RETURN [end IN l WHERE end > 0 | end]") == ListComprehension(
        "end", v("l"), Binary(BinaryOperator.GREATER_THAN, v("end"), Literal(0)), v("end")
    )
    assert returned("WITH [true] AS l RETURN all(all IN l WHERE all)") == Quantifier(
        QuantifierKind.ALL, "all", v("l"), v("all")
    )
    assert returned("WITH [1] AS l RETURN reduce(end = 0, in IN l | end + in)") == Reduce(
        "end", Literal(0), "in", v("l"), Binary(BinaryOperator.ADD, v("end"), v("in"))
    )
    assert returned("MATCH (end) RETURN end {.name}") == MapProjection("end", (Selected("name"),))
    assert returned("MATCH (n), (end) RETURN n {end, case: end}") == MapProjection(
        "n", (VariableElement("end"), Entry("case", v("end")))
    )
    comprehension = returned("MATCH (a) RETURN [order = (a)-[case]->(end) WHERE end.x = 1 | end.name]")
    assert isinstance(comprehension, PatternComprehension)
    assert comprehension.pattern.variable == "order"
    assert [it.variable for it in comprehension.pattern.nodes] == ["a", "end"]
    assert [it.variable for it in comprehension.pattern.relationships] == ["case"]


def test_a_keyword_that_names_a_variable_beside_the_same_keyword_reads_as_neo4j_reads_it():
    v = Variable

    def projection(cypher: str) -> Projection:
        last = read_cypher(cypher).clauses[-1]
        assert isinstance(last, Return)
        return last.projection

    distinct = (ProjectionItem(v("distinct"), "distinct"),)
    assert projection("UNWIND [1] AS distinct RETURN distinct") == Projection(False, False, distinct, (), None, None)
    assert projection("UNWIND [1] AS distinct RETURN DISTINCT distinct") == Projection(
        True, False, distinct, (), None, None
    )
    assert projection("UNWIND [1] AS desc RETURN desc ORDER BY desc DESC").order_by == (SortItem(v("desc"), True),)
    assert projection("UNWIND [1] AS asc RETURN asc ORDER BY asc").order_by == (SortItem(v("asc"), False),)
    with_ = read_cypher("UNWIND [1] AS order WITH order ORDER BY order RETURN order").clauses[1]
    assert isinstance(with_, With) and with_.projection.order_by == (SortItem(v("order"), False),)
    paged = projection("UNWIND [1] AS skip WITH skip AS limit RETURN limit SKIP 0 LIMIT 1")
    assert (paged.skip, paged.limit) == (Literal(0), Literal(1))
    # count(distinct) is count() of DISTINCT and nothing, as it was before a keyword could name a variable.
    assert returned("UNWIND [1] AS x RETURN count(distinct)") == Function("count", True, ())
    assert returned("UNWIND [1] AS distinct RETURN count(DISTINCT distinct)") == Function(
        "count", True, (v("distinct"),)
    )
    where = read_cypher("MATCH (where) WHERE where.x = 1 RETURN where").clauses[0]
    assert isinstance(where, Match)
    assert [it.variable for it in where.patterns[0].nodes] == ["where"]
    assert where.where == Binary(BinaryOperator.EQUALS, Property(v("where"), "x"), Literal(1))
    assert returned("WITH 1 AS end, 0 AS then RETURN CASE end WHEN 1 THEN end ELSE then END") == Case(
        v("end"), ((Literal(1), v("end")),), v("then")
    )
    exists = [
        it.expression for it in projection("UNWIND [1] AS exists RETURN exists, EXISTS { MATCH (exists) } AS e").items
    ]
    assert exists[0] == v("exists")
    assert isinstance(exists[1], Subquery) and exists[1].kind is SubqueryKind.EXISTS
    all_ = [it.expression for it in projection("UNWIND [[true]] AS all RETURN all, all(x IN all WHERE x) AS a").items]
    assert all_ == [v("all"), Quantifier(QuantifierKind.ALL, "x", v("all"), v("x"))]
    union = read_cypher(
        "UNWIND [1] AS union RETURN union UNION ALL UNWIND [2] AS all RETURN all AS union UNION RETURN 3 AS union"
    )
    assert [it.all for it in union.unions] == [True, False]
    first = union.unions[0].clauses[-1]
    assert isinstance(first, Return) and first.projection.items[0].expression == v("all")
    assert returned("UNWIND [true] AS not RETURN NOT not") == Unary(UnaryOperator.NOT, v("not"))
    assert returned("UNWIND [1] AS is RETURN is IS NOT NULL") == Unary(UnaryOperator.IS_NOT_NULL, v("is"))
    assert returned("UNWIND [[1]] AS in RETURN 1 IN in") == Binary(BinaryOperator.IN, Literal(1), v("in"))
    (merge,) = read_cypher("MERGE (on:A) ON CREATE SET on.a = 1 ON MATCH SET on.b = 2").clauses
    assert isinstance(merge, Merge)
    assert (merge.on_create, merge.on_match) == (
        (SetProperty("on", "a", Literal(1)),),
        (SetProperty("on", "b", Literal(2)),),
    )
    assert read_cypher("MATCH (detach) DETACH DELETE detach").clauses[-1] == Delete(True, (v("detach"),))


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
