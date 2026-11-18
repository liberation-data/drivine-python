"""
A view's path ordered by a hop's property is ordered on SQLite, Postgres and Oracle by the shared module's order
fragments over the hop's node, as drivine4j's `pathOrder` orders it (#540; Drivine for TypeScript's
SqlViewPathOrder.spec.ts), and not by the library's own SQL of kind, then number, then text, which sorted a date by its
tag's text and kinds out of Cypher's order.
"""

import re
from collections.abc import Callable
from typing import Annotated

import pytest

from drivine import Direction, GraphPath, GraphView, ModelRegistry, NodeFragment, NodeId, Root, hop
from drivine.backend.graph_backend import GraphBackend
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.sql.fragments import Fragments
from drivine.backend.sqlite.sqlite_backend import SqliteBackend
from drivine.backend.sqlite.sqlite_graph import SqliteGraph
from drivine.cypher.shared_compiler import SharedStore


class PathOrderActor(NodeFragment, labels=["PathOrderActor"]):
    id: Annotated[str, NodeId()]


class PathOrderDirector(NodeFragment, labels=["PathOrderDirector"]):
    id: Annotated[str, NodeId()]


class DirectorsByRelease(GraphView):
    actor: Annotated[PathOrderActor, Root()]
    directors: Annotated[
        list[PathOrderDirector],
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "released"), hop("DIRECTED_BY")]),
    ] = []
    latest: Annotated[
        PathOrderDirector | None,
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "released", False), hop("DIRECTED_BY")]),
    ] = None


STORES: list[tuple[SharedStore, Callable[[], GraphBackend]]] = [
    ("SQLITE", lambda: SqliteBackend(SqliteGraph())),
    ("POSTGRES", lambda: PostgresBackend(PostgresGraph())),
    ("ORACLE", lambda: OracleBackend(OracleGraph())),
]

ROWS = re.compile(r"\b[kswr]\d+_\.")
"""Each row the module's terms read, a hop node joined back or a way: written `ROW_.` for the checks."""


def module_order(store: SharedStore, descending: bool) -> str:
    """
    The module's order of a hop node `ROW_` by `released`, as a path reads it: descending where asked, no value last.
    """
    return ", ".join(Fragments(store, "drivine").ordered("n.released", descending, "LAST", row="ROW_"))


@pytest.mark.parametrize("store, backend_of", STORES, ids=[it[0] for it in STORES])
class TestAViewsPathOrderedByAHop:
    def test_orders_the_nodes_reached_by_the_modules_terms_over_the_hop_node_each_keeps_then_by_id(
        self, store: SharedStore, backend_of: Callable[[], GraphBackend]
    ):
        text = ROWS.sub("ROW_.", backend_of().load_view(ModelRegistry().view(DirectorsByRelease)).text)
        assert f"ORDER BY {module_order(store, False)}, x" in text
        assert f"ORDER BY {module_order(store, True)}, x" in text

    def test_keeps_for_each_node_reached_its_first_hop_node_in_the_modules_order(
        self, store: SharedStore, backend_of: Callable[[], GraphBackend]
    ):
        text = ROWS.sub("ROW_.", backend_of().load_view(ModelRegistry().view(DirectorsByRelease)).text)
        order = module_order(store, False)
        first = {
            "SQLITE": f"json_group_array(ROW_.nid ORDER BY {order}) ->> '$[0]' AS hop",
            "POSTGRES": f"(array_agg(ROW_.nid ORDER BY {order}))[1] AS hop",
            "ORACLE": f"FIRST_VALUE(ROW_.hop) OVER (PARTITION BY ROW_.nid ORDER BY {order}) AS hop",
        }[store]
        assert first in text

    def test_reads_the_property_neither_as_a_number_and_a_text_of_the_librarys_own_nor_by_its_kind(
        self, store: SharedStore, backend_of: Callable[[], GraphBackend]
    ):
        text = backend_of().load_view(ModelRegistry().view(DirectorsByRelease)).text
        assert re.search(r"order_kind|order_number|order_text", text) is None

    def test_orders_a_views_path_the_same_way_in_a_search_whose_root_is_the_view(
        self, store: SharedStore, backend_of: Callable[[], GraphBackend]
    ):
        model = ModelRegistry().view(DirectorsByRelease)
        found = backend_of().nearest(model, NearestSearch("PathOrderActor", "embedding", "COSINE", 5))
        searched = ROWS.sub("ROW_.", found.text)
        assert f"ORDER BY {module_order(store, False)}, x" in searched
        assert f"ORDER BY {module_order(store, True)}, x" in searched
        if store == "ORACLE":
            assert re.match(r"SELECT /\*\+ NO_JSON_TABLE_TRANSFORM \*/ JSON_OBJECT\('value'", found.text)


class TestAViewsPathOrderedByAHopOnOracle:
    def test_carries_a_filters_hints_and_calls_beside_the_views_once_each_on_its_outermost_select(self):
        where = "/*drivine:hint NO_JSON_TABLE_TRANSFORM*//*drivine:hint OTHER*//*drivine:calls era*/1 = 1"
        filtered = OracleBackend(OracleGraph()).load_all_views(ModelRegistry().view(DirectorsByRelease), where)
        assert re.match(r"SELECT /\*\+ NO_JSON_TABLE_TRANSFORM OTHER \*/ JSON_OBJECT", filtered.text)
        assert "/*drivine:" not in filtered.text
        assert filtered.calls is not None and filtered.calls.era

    def test_carries_the_hints_the_fragments_ask_for_on_its_outermost_select_and_joins_the_hop_node_back_by_its_keys(
        self,
    ):
        text = OracleBackend(OracleGraph()).load_view(ModelRegistry().view(DirectorsByRelease)).text
        assert re.match(r"SELECT /\*\+ NO_JSON_TABLE_TRANSFORM \*/ JSON_OBJECT", text)
        assert re.search(
            r"JOIN drivine_node (k\d+_) ON \(\1\.nid = (o\d+)\.hop \+ 0 AND \2\.hop = \1\.nid \+ 0\)", text
        )
