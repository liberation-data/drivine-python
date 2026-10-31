"""
On Oracle, two texts are compared as `VARCHAR2`s, or as CLOBs by `DBMS_LOB.COMPARE` where either is past 4,000 bytes,
and not as JSON: `=` of JSON refused a text past about 32 KB (ORA-40180), and `<`, `>`, `<=` and `>=` of one taken out
of the store as JSON on two rows or more closed the session (ORA-03113, #291). A property is read by its own path.
drivine4j's OracleCompareTextTest, ported as Drivine for TypeScript's OracleCompareText.spec.ts.
"""

import re
from typing import Any

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler


def sql(cypher: str, parameters: dict[str, Any] | None = None) -> str:
    return OracleCypherCompiler("g").compile(cypher, {"x": "abc"} if parameters is None else parameters).sql


def clobs(cx: str, cy: str, operator: str) -> str:
    """The comparison `operator` of two texts as CLOBs, `cx` and `cy`, each made a CLOB by `TO_CLOB`."""
    return (
        f"NVL(DBMS_LOB.COMPARE(TO_CLOB({cx}), TO_CLOB({cy})), "
        f"SIGN(NVL(LENGTH(TO_CLOB({cx})), 0) - NVL(LENGTH(TO_CLOB({cy})), 0))) {operator} 0"
    )


def texts(sx: str, sy: str, cx: str, cy: str, operator: str) -> str:
    """The comparison `operator` of two texts as their `VARCHAR2`s `sx` and `sy`, where both are read so, else as their
    CLOBs `cx` and `cy`."""
    whole = clobs(cx, cy, operator)
    return f"CASE WHEN {sx} IS NOT NULL AND {sy} IS NOT NULL THEN {sx} {operator} {sy} ELSE {whole} END"


def property_(node: str, clob: bool = False) -> str:
    return f"JSON_VALUE({node}.props, '$.t'{' RETURNING CLOB' if clob else ''})"


ITEM = re.compile(
    re.escape(f"({property_('n1')} IS NOT NULL OR NVL(JSON_VALUE(n1.props, '$.t.type()'), 'null') <> 'string') AND ")
    + r"EXISTS \(SELECT 1 FROM .*? i\d+ WHERE i\d+\.item = "
    + re.escape("JSON_QUERY(n1.props, '$.t' RETURNING JSON))")
)
"""The property tested once, read as a `VARCHAR2` or no text, and then compared with each item of a list as JSON, a
plain equality."""

CLOB_ITEM = re.compile(
    r"DBMS_LOB\.COMPARE\(TO_CLOB\(JSON_VALUE\(i\d+\.item, '\$' RETURNING CLOB\)\), "
    r"TO_CLOB\(JSON_VALUE\(n1\.props, '\$\.t' RETURNING CLOB\)\)\)"
)
"""An item of a list compared with the property as a text, each as a CLOB."""


def test_two_stored_texts_are_ordered_and_compared_as_varchar2s_or_clobs_each_read_by_its_path_others_as_json():
    n = [property_("n1"), property_("n2"), property_("n1", True), property_("n2", True)]
    for operator in ["<", ">", "<=", ">="]:
        ordered = sql(f"MATCH (n:Doc), (m:Doc) WHERE n.t {operator} m.t RETURN count(*)")
        text = texts(n[0], n[1], n[2], n[3], operator)
        assert (
            f"THEN CASE WHEN JSON_VALUE(n1.props, '$.t.type()') = 'string' THEN {text} ELSE "
            f"JSON_QUERY(n1.props, '$.t' RETURNING JSON) {operator} JSON_QUERY(n2.props, '$.t' RETURNING JSON) END END"
        ) in ordered, ordered
        assert "JSON_VALUE(JSON_QUERY(" not in ordered, ordered
    # Equal as JSON where both are read as VARCHAR2s, as is quicker than reading their kinds; else as CLOBs where both
    # are texts, null where either isn't there, and not equal where one is a text.
    equal = sql("MATCH (n:Doc), (m:Doc) RETURN n.t = m.t, n.t <> m.t")
    json = "JSON_QUERY(n1.props, '$.t' RETURNING JSON) = JSON_QUERY(n2.props, '$.t' RETURNING JSON)"

    def kinds(and_: str) -> str:
        return f"JSON_VALUE(n1.props, '$.t.type()') = 'string' {and_} JSON_VALUE(n2.props, '$.t.type()') = 'string'"

    compared = (
        f"CASE WHEN {n[0]} IS NOT NULL AND {n[1]} IS NOT NULL THEN {json} "
        f"WHEN {kinds('AND')} THEN {clobs(n[2], n[3], '=')} "
        "WHEN JSON_VALUE(n1.props, '$.t.type()') IS NULL OR JSON_VALUE(n2.props, '$.t.type()') IS NULL THEN NULL "
        f"WHEN {kinds('OR')} THEN FALSE ELSE {json} END"
    )
    assert equal.count(compared) == 2, equal
    assert "JSON_VALUE(JSON_QUERY(" not in equal, equal
    # A query of one table that compares a property is not rewritten by Oracle's JSON table transformation, which lost
    # the reads of a node's properties beside a quantifier's subquery; one that compares none is, as before.
    for cypher in [
        "MATCH (n:Doc) WHERE n.t > $x RETURN n",
        "MATCH (n:Doc) RETURN all(x IN [1] WHERE n.b > 1962) = true",
    ]:
        assert sql(cypher).startswith("SELECT /*+ NO_JSON_TABLE_TRANSFORM */"), cypher
    assert "NO_JSON_TABLE_TRANSFORM" not in sql("MATCH (n:Doc) RETURN n.t")


def test_a_stored_text_is_compared_as_a_text_with_a_given_one_the_items_of_a_list_and_a_long_value_bound_by_its_path():
    # A given text of at most 4,000 bytes is read as a VARCHAR2, and so is equal to no stored text that isn't, which is
    # longer or empty.
    given = sql("MATCH (n:Doc) WHERE n.t = $x RETURN n")
    assert (
        f"CASE WHEN {property_('n1')} IS NOT NULL OR NVL(JSON_VALUE(n1.props, '$.t.type()'), 'null') <> 'string' "
        "THEN JSON_QUERY(n1.props, '$.t' RETURNING JSON) = JSON($_cy_x) ELSE FALSE END"
    ) in given, given
    longer = sql("MATCH (n:Doc) WHERE n.t = $x RETURN n", {"x": "€" * 1_334})
    given_clob = "JSON_VALUE(JSON($_cy_x), '$' RETURNING CLOB)"
    assert f"THEN {clobs(property_('n1', True), given_clob, '=')}" in longer, longer
    for written, json in [("5", "JSON('5')"), ("true", "JSON('true')")]:
        compiled = sql(f"MATCH (n:Doc) WHERE n.t = {written} RETURN n")
        assert f"<> 'string' THEN JSON_QUERY(n1.props, '$.t' RETURNING JSON) = {json} ELSE FALSE END" in compiled, (
            compiled
        )
    # A value a WITH passes on may be JSON's null, which is tested first, then compared as a stored one is.
    passed = sql("MATCH (n:Doc) WITH n.t AS t WHERE t = $x RETURN t")
    found = re.search(r"CASE WHEN NOT NVL\(JSON_VALUE\((w\d+\.c1), '\$\.type\(\)'\), 'null'\) = 'null' THEN", passed)
    assert found is not None, passed
    t = found[1]
    assert (
        f"THEN CASE WHEN JSON_VALUE({t}, '$') IS NOT NULL OR NVL(JSON_VALUE({t}, '$.type()'), 'null') <> 'string' "
        f"THEN {t} = JSON($_cy_x) ELSE FALSE END END"
    ) in passed, passed
    empty = sql("MATCH (n:Doc) WHERE n.t = '' RETURN n")
    empty_clob = """JSON_VALUE(JSON('""'), '$' RETURNING CLOB)"""
    assert f"THEN {clobs(property_('n1', True), empty_clob, '=')}" in empty, empty
    assert "ELSE FALSE END" not in empty, empty
    # Items each read as a VARCHAR2, written or given, are compared as JSON with the property, where it is read as one
    # too or is no text, which is tested once, not for each item; a longer text is in none of them.
    for cypher in [
        "MATCH (n:Doc) WHERE n.t IN $xs RETURN n",
        "MATCH (n:Doc) RETURN n.t IN $xs",
        "MATCH (n:Doc) RETURN n.t IN ['a', 5, $x]",
    ]:
        listed = sql(cypher, {"xs": ["a", "b"], "x": "abc"})
        assert ITEM.search(listed) is not None and "DBMS_LOB" not in listed, f"{cypher}: {listed}"
    # So are those of a given list that holds a null, which is compared whole, not item by item. Where the property is
    # in no item, a RETURN is null, as the comparison with the null item is: no item is read for that, nor is the
    # property read for its nulls, which took a text past 32 KB out as JSON and closed the session.
    for cypher in ["MATCH (n:Doc) WHERE n.t IN $xs RETURN n", "MATCH (n:Doc) RETURN n.t IN $xs"]:
        listed = sql(cypher, {"xs": ["a", None]})
        assert ITEM.search(listed) is not None, f"{cypher}: {listed}"
        assert CLOB_ITEM.search(listed) is None and "JSON_SERIALIZE" not in listed, f"{cypher}: {listed}"
    assert ") THEN TRUE ELSE NULL END" in sql("MATCH (n:Doc) RETURN n.t IN $xs", {"xs": ["a", None]})
    # Where none is null, or there is none, it is false, a value that holds a null too, which is not read for it.
    for cypher, xs in [("MATCH (n:Doc) RETURN n.t IN $xs", []), ("MATCH (n:Doc) RETURN [n.t, null] IN $xs", ["a", 5])]:
        listed = sql(cypher, {"xs": xs})
        assert ") THEN TRUE ELSE FALSE END" in listed, f"{cypher}: {listed}"
        assert re.search(r"JSON_SERIALIZE|nulls_equal", listed) is None, f"{cypher}: {listed}"
    assert ") THEN TRUE ELSE NULL END" in sql("MATCH (n:Doc) RETURN [n.t, null] IN $xs", {"xs": ["a", None]})
    passed_on = sql("MATCH (n:Doc) WITH n.t AS t RETURN t IN $xs", {"xs": ["a", None]})
    assert re.search(r"JSON_SERIALIZE\(w\d+\.c1", passed_on) is None, passed_on
    # A list written out that holds a null is compared item by item, each item as equality compares it: the written
    # ones, the null among them, as a given text or number is.
    itemwise = sql("MATCH (n:Doc) RETURN n.t IN ['a', 5, null, $x]")
    assert itemwise.count("NVL(JSON_VALUE(n1.props, '$.t.type()'), 'null') <> 'string' THEN") == 4, itemwise
    assert "DBMS_LOB" not in itemwise, itemwise
    # An empty item, or one past 4,000 bytes, isn't, and both sides are compared as texts.
    for xs in [["a", ""], ["a", "€" * 1_334]]:
        listed = sql("MATCH (n:Doc) WHERE n.t IN $xs RETURN n", {"xs": xs})
        assert CLOB_ITEM.search(listed) is not None and ITEM.search(listed) is None, listed
    # The issue's: beside a value too long to write twice, which is bound, the property is bound by its own path, its
    # JSON only where it is no text past 32,767 characters, and its kind and CLOB from columns of their own.
    beside = sql(
        "MATCH (n:Doc), (m:Doc) WHERE n.t > toLower(trim(coalesce($p.q, m.name))) RETURN count(*)", {"p": {"q": None}}
    )
    assert (
        "CASE WHEN JSON_VALUE(n1.props, '$.t.length()' RETURNING NUMBER) > 32767 THEN NULL "
        "ELSE JSON_QUERY(n1.props, '$.t' RETURNING JSON) END AS v1, JSON_VALUE(n1.props, '$.t.type()') AS v2, "
        "JSON_VALUE(n1.props, '$.t' RETURNING CLOB) AS v3"
    ) in beside, beside
    bound = re.search(r"(sh\d+)\.v3", beside)
    assert bound is not None, beside
    alias = bound[1]
    ordered = texts(
        f"JSON_VALUE({alias}.v1, '$')",
        f"JSON_VALUE({alias}.v4, '$')",
        f"{alias}.v3",
        f"JSON_VALUE({alias}.v4, '$' RETURNING CLOB)",
        ">",
    )
    assert f"THEN CASE WHEN {alias}.v2 = 'string' THEN {ordered} ELSE {alias}.v1 > {alias}.v4 END" in beside, beside
    in_beside = sql("MATCH (n:Doc), (m:Doc) RETURN n.t IN [toLower(trim(coalesce($p.q, m.name)))]", {"p": {"q": None}})
    assert "JSON_VALUE(n1.props, '$.t' RETURNING CLOB) AS v3" in in_beside, in_beside
    assert "DBMS_LOB.COMPARE(" in in_beside, in_beside

    # A value bound beside the list, which may be a text past 32 KB, and one a WITH passes on, which may be JSON's null,
    # are tested once too, beside the EXISTS, which compares each item with the bound name as JSON.
    def tested(v: str) -> str:
        short = f"JSON_VALUE({v}, '$') IS NOT NULL OR NVL(JSON_VALUE({v}, '$.type()'), 'null') <> 'string'"
        return f"({short}) AND EXISTS ("

    bound_value = sql(
        "MATCH (n:Doc), (m:Doc) WHERE toLower(trim(coalesce($p.q, m.name))) IN ['a', 'b'] RETURN n", {"p": {"q": None}}
    )
    v = re.search(r"WHERE i\d+\.item = (sh\d+\.v\d+)\)", bound_value)
    assert v is not None and tested(v[1]) in bound_value, bound_value
    passed_in = sql("MATCH (n:Doc) WITH n.t AS t WHERE t IN $xs RETURN t", {"xs": ["a", None]})
    w = re.search(r"WHERE i\d+\.item = (w\d+\.c1)\)", passed_in)
    assert w is not None, passed_in
    assert f"({tested(w[1])}" in passed_in, passed_in
    assert f"AND NOT NVL(JSON_VALUE({w[1]}, '$.type()'), 'null') = 'null')" in passed_in, passed_in


def test_a_stored_text_is_compared_as_a_text_with_each_item_of_a_list_a_subquery_works_out_for_each_row():
    # The list worked out in a derived table inside a lateral view (#272), and the property read by its own path.
    filtered = sql("MATCH (n:Doc) WHERE n.t IN [(n)-->(q) | q.t] RETURN n")
    assert re.search(r"LATERAL \(SELECT i\d+_v\.list FROM \(SELECT \(SELECT", filtered) is not None, filtered
    assert CLOB_ITEM.search(filtered) is not None, filtered
    assert "JSON_VALUE(JSON_QUERY(" not in filtered, filtered
    # Beside such a list, too long to write twice and so bound, the property is bound by its own path, as beside a sum.
    returned = sql("MATCH (n:Doc), (m:Doc) RETURN n.t IN keys(m)")
    bound = re.search(r"JSON_VALUE\(n1\.props, '\$\.t' RETURNING CLOB\) AS v4\) (sh\d+)", returned)
    assert bound is not None, returned
    item = r"TO_CLOB\(JSON_VALUE\(i\d+\.item, '\$' RETURNING CLOB\)\)"
    compared = rf"DBMS_LOB\.COMPARE\({item}, TO_CLOB\({bound[1]}\.v4\)\)"
    assert re.search(compared, returned) is not None, returned


def test_the_items_of_a_list_that_holds_a_list_or_a_map_written_or_given_are_compared_with_a_stored_text_as_texts():
    for cypher, parameters in [
        ("MATCH (n:Doc) WHERE n.t IN ['a', $m] RETURN n", {"m": {"k": 1}}),
        ("MATCH (n:Doc) WHERE n.t IN ['a', ['b']] RETURN n", {}),
        ("MATCH (n:Doc) WHERE n.t IN $xs RETURN n", {"xs": ["a", ["b"]]}),
    ]:
        listed = sql(cypher, parameters)
        assert ITEM.search(listed) is None and "DBMS_LOB.COMPARE(" in listed, f"{cypher}: {listed}"


def test_two_values_the_statement_writes_are_compared_as_json_and_beside_one_only_the_other_side_is_tested():
    # Each item of a list written out that holds a null and values of several kinds is compared with the value alone:
    # in a few hundred characters each, as before, where Oracle ran out of memory reading more.
    literals = sql("RETURN [7 IN [0, 's1', null, 3.5, true, [5, 'n'], {k: 6}], 'a' = 'a', [1] = [1, null]]")
    assert "DBMS_LOB" not in literals and "<> 'string'" not in literals, literals
    assert "JSON('7') = JSON_SCALAR(TO_BINARY_DOUBLE('3.5'))" in literals, literals
    # Beside a written value that is no text or a short text, the other side alone is tested; beside an empty text,
    # which Oracle reads as null, or one past 4,000 bytes, both are, as texts.
    one = (
        "CASE WHEN JSON_VALUE(n1.props, '$.t') IS NOT NULL OR NVL(JSON_VALUE(n1.props, '$.t.type()'), 'null') <> "
        "'string' THEN JSON_QUERY(n1.props, '$.t' RETURNING JSON) = "
    )
    for written, value in [
        ("'a'", "JSON('\"a\"')"),
        ("[1, 2]", "JSON_ARRAY("),
        ("{k: 1}", "JSON_OBJECT("),
        ("1.5", "JSON_SCALAR(TO_BINARY_DOUBLE("),
    ]:
        compared = sql(f"MATCH (n:Doc), (m:Doc) RETURN m.t = n.t, {written} = n.t")
        assert one + value in compared and "DBMS_LOB" in compared, f"{written}: {compared}"
    # A float worked out, unlike one written, may be SQL's null, so is compared as any other value: null beside a long
    # text, not false.
    worked = sql("MATCH (n:Doc) RETURN toFloat(n.y) = n.t")
    assert one not in worked and "IS NULL THEN NULL WHEN" in worked, worked
    for written in ["''", f"'{'x' * 4_001}'"]:
        compared = sql(f"MATCH (n:Doc) RETURN {written} = n.t")
        assert one not in compared and "DBMS_LOB" in compared, f"{len(written)}: {compared}"


def test_two_texts_the_statement_makes_or_is_given_are_compared_as_texts_and_postgres_writes_a_comparison_as_it_was():
    made = sql("WITH $t AS t, $x AS x RETURN substring(t, 0, 32768) = x", {"t": "abc", "x": "a"})
    assert "DBMS_LOB.COMPARE(" in made, made
    assert re.search(r"THEN sh\d+\.v1 = sh\d+\.v2 END", made) is None, made
    compiled = (
        PostgresCypherCompiler("g")
        .compile("MATCH (n:Doc), (m:Doc) WHERE n.t > m.t AND n.t = $x RETURN n.t IN [m.t, $x]", {"x": "abc"})
        .sql
    )
    assert "DBMS_LOB" not in compiled and "RETURNING CLOB" not in compiled, compiled


def test_two_values_are_compared_as_json_as_before_in_a_statement_that_writes_and_in_the_step_of_a_reduce():
    # There a CLOB read from an unwound row's JSON closed the session, where the JSON is refused (ORA-40180).
    written = sql(
        "UNWIND $items AS t MERGE (n:Doc {a: t.a}) WITH n WHERE n.t > $x AND n.t IN $xs SET n.y = 1",
        {"items": [{"a": "x"}], "x": "abc", "xs": ["a"]},
    )
    assert "DBMS_LOB.COMPARE" not in written, written
    assert re.search(r"THEN JSON_QUERY\(w\d+\.c1_props, '\$\.t' RETURNING JSON\) > CASE WHEN", written), written
    folded = sql("WITH ['a', 'b'] AS l RETURN reduce(acc = [], s IN l | acc + [s = $x, s < $x])")
    assert "DBMS_LOB.COMPARE" not in folded, folded
