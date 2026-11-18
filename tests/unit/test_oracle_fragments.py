"""
What the shared module's fragments on Oracle ask of the statement they are written into travels with their text, and
is written in there (oracle_fragments): the hints after the statement's outermost `SELECT` or `DELETE`, and the stored
functions they call said. Drivine for TypeScript's OracleFragments of liberation-data/drivine#797.
"""

from drivine.backend.graph_statement import StoredFunctionCalls
from drivine.backend.oracle.oracle_fragments import called, hinted, noted
from drivine.backend.sql.fragments import Fragments


def fragments(*cypher: str) -> Fragments:
    """The fragments of a statement over `Person`, whose properties are indexed for it, each condition written."""
    written = Fragments("ORACLE", "g", "n_", ["Person"], [("Person", "name")])
    for i, it in enumerate(cypher):
        written.where(it, {f"_q{i}": {"a": None} if "meta" in it else "Ada"})
    return written


def test_noted_puts_each_hint_and_each_function_called_before_the_text_and_nothing_where_none_is_asked():
    asked = fragments("n.name = $_q0", "n.meta = $_q1")
    assert noted("x = 1", asked) == ("/*drivine:hint NO_JSON_TABLE_TRANSFORM*//*drivine:calls nullsEqual*/x = 1")
    assert noted(None, asked) is None
    assert noted("x = 1", Fragments("POSTGRES", "g")) == "x = 1"


def test_hinted_writes_each_hint_once_in_the_order_asked_after_the_outermost_select_or_delete():
    where = noted("x = 1", fragments("n.name = $_q0"))
    assert hinted(f"SELECT a FROM t WHERE {where} AND (SELECT 1 FROM d) = 1") == (
        "SELECT /*+ NO_JSON_TABLE_TRANSFORM */ a FROM t WHERE x = 1 AND (SELECT 1 FROM d) = 1"
    )
    # Asked twice, written once; a statement's own first.
    assert hinted(f"  delete FROM t WHERE {where} AND {where}", ["MINE"]) == (
        "  delete /*+ MINE NO_JSON_TABLE_TRANSFORM */ FROM t WHERE x = 1 AND x = 1"
    )
    assert hinted("SELECT 1") == "SELECT 1"
    # A block's statements are hinted each before it is written into the block: its own keyword is the first.
    assert hinted(f"BEGIN SELECT COUNT(*) INTO v FROM t WHERE {where}; END;") == (
        "BEGIN SELECT /*+ NO_JSON_TABLE_TRANSFORM */ COUNT(*) INTO v FROM t WHERE x = 1; END;"
    )


def test_hinted_leaves_what_the_fragments_call_and_called_takes_it_out_saying_which_they_are():
    where = noted("x = 1", fragments("n.name = $_q0", "n.meta = $_q1"))
    text = hinted(f"SELECT a FROM t WHERE {where}")
    assert text == "SELECT /*+ NO_JSON_TABLE_TRANSFORM */ a FROM t WHERE /*drivine:calls nullsEqual*/x = 1"
    assert called(text, "g") == (
        "SELECT /*+ NO_JSON_TABLE_TRANSFORM */ a FROM t WHERE x = 1",
        StoredFunctionCalls("g", nulls_equal=True),
    )
    assert called("SELECT 1", "g") == ("SELECT 1", None)
    # With those of the statement's own fragments.
    own = fragments("n.meta = $_q0")
    assert called("SELECT 1", "g", own) == ("SELECT 1", StoredFunctionCalls("g", nulls_equal=True))


def test_a_hint_and_a_call_in_a_users_text_that_merely_look_alike_are_left_as_they_are():
    # Only the comments noted writes are read: a text holding `drivine:hint` elsewhere is no note.
    assert hinted("SELECT '/*drivine:hint' FROM dual") == "SELECT '/*drivine:hint' FROM dual"
    assert called("SELECT 'drivine:calls era' FROM dual", "g") == ("SELECT 'drivine:calls era' FROM dual", None)
