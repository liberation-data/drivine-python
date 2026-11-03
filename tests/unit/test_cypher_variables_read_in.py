"""
`variables_read_in`, which tells a pattern comprehension whose body reads none of its pattern's own variables (#305): a
name a comprehension, a quantifier or a reduce binds for itself is not read of the scope, but its list is. Drivine for
TypeScript's VariablesReadIn.spec.ts, ported.
"""

from drivine.cypher.ast.clause import Return
from drivine.cypher.cypher_reader import read_cypher
from drivine.cypher.sql.sql_cypher_compiler import variables_read_in


def read_in(expression: str) -> list[str]:
    """The variables the one item of `RETURN <expression>` reads of the scope it is in, sorted."""
    last = read_cypher(f"RETURN {expression} AS x").clauses[-1]
    assert isinstance(last, Return)
    return sorted(variables_read_in(last.projection.items[0].expression))


def test_a_name_a_binder_binds_for_itself_is_left_out_and_its_list_is_read_of_the_scope():
    assert read_in("[m IN [p.name] | m]") == ["p"]
    assert read_in("[m IN [m.name] | m]") == ["m"]
    assert read_in("[x IN [1] WHERE m IS NOT NULL | p]") == ["m", "p"]
    assert read_in("any(m IN [p.name] WHERE m = 1)") == ["p"]
    assert read_in("any(m IN [m.name] WHERE m = 1)") == ["m"]
    assert read_in("reduce(m = '', x IN [1] | m + x + p.name)") == ["p"]
    assert read_in("reduce(acc = m.name, x IN [n] | acc)") == ["m", "n"]


def test_a_patterns_variables_are_read_and_a_map_projections():
    assert read_in("[(p)-->(m) | [m IN [q] | m]]") == ["m", "p", "q"]
    assert read_in("EXISTS { (p)-->(m) }") == ["m", "p"]
    assert read_in("p {.name, r, n: [r IN [1] | r]}") == ["p", "r"]
