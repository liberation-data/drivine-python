"""
Full-text search is the shared module's (drivine-cypher-spec#100): each SQL store joins the module's ranked candidates
back to its node table for its own projection, and binds the module's parameters; each graph engine runs the module's
head before its own filters and projection. What the module writes is pinned there, by drivine4j's statements. As
Drivine for TypeScript's FullTextSearch.spec.ts.

What stays here is the host side the module's SQL relies on: on SQLite, `drivine_regexp_count`, which counts the terms
the module binds as drivine4j's SqlitePatterns counts them (drivine4j#607, #616).
"""

import re
from collections.abc import Iterator
from typing import Annotated

import apsw
import pytest

from drivine import DrivineError, FullTextIndex, ModelRegistry, NodeFragment, NodeId
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR, CypherGrammar
from drivine.backend.graph_backend import GraphBackend
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.sqlite.sqlite_backend import SqliteBackend
from drivine.backend.sqlite.sqlite_functions import register
from drivine.backend.sqlite.sqlite_graph import REGEXP_COUNT, SqliteGraph
from drivine.backend.sqlite.sqlite_patterns import python_pattern
from drivine.cypher.shared_compiler import (
    SearchIndex,
    SharedStore,
    full_text_index_lookup,
    full_text_search,
)
from drivine.model.fragment_model import FragmentModel


class Shelf(NodeFragment, labels=["Shelf", "Stocked"]):
    id: Annotated[str, NodeId()]
    title: Annotated[str | None, FullTextIndex()] = None


SHELF: FragmentModel = ModelRegistry().fragment(Shelf)
GRAPHS = {"POSTGRES": PostgresGraph(), "ORACLE": OracleGraph(), "SQLITE": SqliteGraph()}
ENGINES = [NEO4J_GRAMMAR, MEMGRAPH_GRAMMAR, FALKORDB_GRAMMAR]


def search(text: str, index: SearchIndex | None = None, limit: int = 5) -> MatchingSearch:
    return MatchingSearch("Shelf", ("title",), limit, text=text, index=index)


STORES: list[tuple[SharedStore, GraphBackend, SearchIndex | None]] = [
    ("POSTGRES", PostgresBackend(GRAPHS["POSTGRES"]), None),
    ("POSTGRES", PostgresBackend(GRAPHS["POSTGRES"]), SearchIndex(trigram=True)),
    ("ORACLE", OracleBackend(GRAPHS["ORACLE"]), None),
    ("ORACLE", OracleBackend(GRAPHS["ORACLE"]), SearchIndex(oracle_text="drivine_t_Shelftitle_1a2b3c")),
    ("SQLITE", SqliteBackend(GRAPHS["SQLITE"]), None),
]


@pytest.mark.parametrize(("store", "backend", "index"), STORES)
def test_a_sql_store_joins_the_modules_candidates_back_to_its_node_table_and_binds_its_parameters(
    store: SharedStore, backend: GraphBackend, index: SearchIndex | None
):
    planned = full_text_search(store, GRAPHS[store].name, ["Shelf", "Stocked"], ["title"], "graph -relational", index)
    statement = backend.matching(SHELF, search("graph -relational", index))
    assert f"FROM ({planned.statement}) s JOIN {GRAPHS[store].nodes} n ON n.nid = s.nid" in statement.text
    assert statement.bindings == planned.parameters
    assert all(re.fullmatch(r"_cy_ft[0-9]+", it) for it in statement.bindings)


def test_postgres_narrows_by_the_trigram_index_only_where_it_is_made():
    backend = PostgresBackend(GRAPHS["POSTGRES"])
    assert " LIKE " in backend.matching(SHELF, search("graph", SearchIndex(trigram=True))).text
    assert " LIKE " not in backend.matching(SHELF, search("graph")).text
    assert " LIKE " not in backend.matching(SHELF, search("graph", SearchIndex(trigram=False))).text


@pytest.mark.parametrize(("store", "backend", "index"), STORES)
def test_a_sql_store_raises_the_modules_refusal_of_a_query_with_no_word_to_look_for(
    store: SharedStore, backend: GraphBackend, index: SearchIndex | None
):
    with pytest.raises(DrivineError, match=re.escape("A full-text query needs a word to look for: '-only' has none.")):
        backend.matching(SHELF, search("-only", index))


@pytest.mark.parametrize("grammar", ENGINES, ids=lambda it: it.engine)
def test_a_graph_engine_gives_the_query_to_the_index_as_it_is_bound_ahead_of_the_filters(grammar: CypherGrammar):
    statement = CypherBackend(grammar).matching(SHELF, search("-only"))
    assert statement.bindings == {"_cy_ft1": "-only"}
    assert statement.text.index("$_cy_ft1") < statement.text.index("MATCH (n) WHERE")
    # The root's labels beyond the first are checked before the scale, so the index is asked for ten times as many.
    assert "WITH node, score WHERE node:`Shelf`:`Stocked`\nWITH node, score\nORDER BY score DESC\nLIMIT 50\n" in (
        statement.text
    )


@pytest.mark.parametrize("grammar", ENGINES, ids=lambda it: it.engine)
def test_a_graph_engines_index_limit_is_written_as_a_whole_number_never_bound_however_large(grammar: CypherGrammar):
    # As Drivine for TypeScript writes it, whose numbers go over Bolt as floats, which the engines refuse as a LIMIT.
    statement = CypherBackend(grammar).matching(SHELF, search("graph", limit=123_456_789))
    assert list(statement.bindings) == ["_cy_ft1"]
    limits = re.findall(r"LIMIT (\S+)", statement.text)
    assert limits and all(re.fullmatch(r"[0-9]+", it) for it in limits), limits
    # Ten times the hits where the root has labels beyond the first: 1,234,567,890, whole.
    assert "LIMIT 1234567890\n" in statement.text
    assert "$_cy_ft2" not in statement.text


def test_the_full_text_index_is_looked_up_in_the_catalog_as_the_module_does_on_the_stores_that_record_it():
    postgres = PostgresBackend(GRAPHS["POSTGRES"]).full_text_index("Shelf", ["title"])
    lookup = full_text_index_lookup("POSTGRES", GRAPHS["POSTGRES"].name, "Shelf", ["title"])
    assert lookup is not None
    assert postgres.statement.text == lookup.statement
    assert f" FROM {GRAPHS['POSTGRES'].catalog} " in postgres.statement.text
    assert postgres.statement.bindings == lookup.parameters
    assert postgres.found([]) == SearchIndex(trigram=False)
    assert postgres.found([{"dimensions": None}]) == SearchIndex(trigram=True)

    oracle = OracleBackend(GRAPHS["ORACLE"]).full_text_index("Shelf", ["title"])
    assert f" FROM {GRAPHS['ORACLE'].catalog} " in oracle.statement.text
    assert oracle.found([]) == SearchIndex()
    assert oracle.found(["drivine_t_Shelftitle_1a2b3c"]) == SearchIndex(oracle_text="drivine_t_Shelftitle_1a2b3c")

    assert getattr(SqliteBackend(GRAPHS["SQLITE"]), "full_text_index", None) is None
    assert full_text_index_lookup("SQLITE", GRAPHS["SQLITE"].name, "Shelf", ["title"]) is None


def test_an_index_found_is_written_as_the_request_has_it_and_nothing_that_was_not_found():
    assert SearchIndex().written() == {}
    assert SearchIndex(trigram=False).written() == {"trigram": False}
    assert SearchIndex(oracle_text="t", name="n").written() == {"oracleText": "t", "name": "n"}


def counting(query: str) -> str:
    """The expression the shared module's SQLite search counts the one term of `query` by: the first value it binds."""
    return full_text_search("SQLITE", "g", ["Doc"], ["body"], query).parameters["_cy_ft1"]


@pytest.fixture(scope="module")
def db() -> Iterator[apsw.Connection]:
    connection = apsw.Connection(":memory:")
    register(connection)
    yield connection
    connection.close()


def counted(db: apsw.Connection, query: str, text: str) -> int:
    """How often the one term of `query` occurs in `text`, by the count the module's search asks of SQLite for it."""
    ((n,),) = db.execute(f"SELECT {REGEXP_COUNT}(?, ?)", (text, counting(query))).fetchall()
    return int(n)


@pytest.mark.parametrize(
    ("char", "in_word"),
    [("क", True), ("ि", True), ("ⅻ", True), ("٣", True), ("_", True), ("²", False), ("½", False), ("-", False)],
)
def test_sqlites_alnum_and_the_modules_words_hold_the_same_characters(char: str, in_word: bool):
    # drivine4j#616: a vowel sign, a letter number and an Arabic-Indic digit are in a word; a superscript and a fraction
    # are not. The text's [[:alnum:]_] on SQLite and the query's words read a character alike, or they disagree. A
    # character between words makes `a…b` the phrase "a b", its words apart by what stands between words.
    assert (re.fullmatch(python_pattern("[[:alnum:]_]"), char) is not None) == in_word
    assert ("]+" not in counting(f"a{char}b")) == in_word


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("kiwi kiwi kiwi", 3),
        ("kiwi,kiwi.kiwi!", 3),
        ("(kiwi)", 1),
        ("kiwi", 1),
        ("kiwis kiwi_kiwi kiwi2 akiwi", 0),
        ("kiwi\nkiwi\n", 2),
    ],
)
def test_a_term_written_again_at_once_is_counted_each_time_wherever_it_stands(db: apsw.Connection, text: str, n: int):
    assert counted(db, "kiwi", text) == n


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("of soup of soup", 2),
        ("of  soup, of -- soup.", 2),
        ("of soups of soup", 1),
        ("of soup_ of soup", 1),
        ("of soup_of soup", 0),
    ],
)
def test_a_phrase_written_again_at_once_is_counted_each_time_its_gap_any_punctuation(
    db: apsw.Connection, text: str, n: int
):
    assert counted(db, '"of soup"', text) == n


def test_a_prefix_term_counts_each_word_it_starts(db: apsw.Connection):
    assert counted(db, "kiw*", "kiwi kiwis kiw akiwi") == 3
    # A `*` inside quotes is text (drivine4j#622): "of sou*" is the phrase "of sou", not in "of soup of soups".
    assert counted(db, '"of sou*"', "of soup of soups") == 0


def test_a_word_in_any_script_is_a_word(db: apsw.Connection):
    assert counted(db, "café", "café café cafés caféé") == 2
    assert counted(db, "кот", "кот кот котик") == 2
    assert counted(db, "किताब", "किताब किताब") == 2
    assert counted(db, "किताब", "किताबें") == 0
    assert counted(db, "bⅻb", "bⅻb bⅻb") == 2


def test_a_phrase_whose_words_begin_it_again_or_whose_first_word_starts_a_later_one_counts_how_often_it_occurs(
    db: apsw.Connection,
):
    assert counted(db, '"la la"', "la la land") == 1
    assert counted(db, '"kiwis kiwi"', "kiwis kiwis kiwi") == 1
    assert counted(db, '"kiwi kiwis"', "kiwi kiwi kiwis kiwis kiwi kiwisx kiwi kiwis") == 2
    assert counted(db, '"ki kiwi bird"', "ki ki kiwi bird ki kiwi birds ki kiwi bird ki kiwi bird") == 3
