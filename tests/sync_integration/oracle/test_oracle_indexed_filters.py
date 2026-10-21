# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_indexed_filters.py:
# change that, and run the script again.
"""
drivine4j#65: Oracle takes a test of a node's labels for a lookup of one row, so a filter on a property with a range
index read every node of the label. A comparison with text that every root meets is now read through the index's
expression, which tests the label too.
"""

from collections.abc import Iterator
from typing import Annotated, Any

import pytest

from drivine.backend.page import Page
from drivine.dsl.query import Query
from drivine.sync import (
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    Root,
    StatelessGraphObjectManager,
    Where,
    any_of,
    range_index,
    refs,
    uniqueness_constraint,
)
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE, OracleSeed


class Issue(NodeFragment, labels=["OraIssue"]):
    id: Annotated[str, NodeId()]
    repo_key: str | int | None = None
    state: str | None = None
    code: str | None = None


class IssueView(GraphView):
    issue: Annotated[Issue, Root()]


ISSUES = 20_000
BY_REPO = range_index("OraIssue", "repoKey")
UNIQUE_CODE = uniqueness_constraint("OraIssue", "code")

q = refs(Issue)


def tidy(gom: StatelessGraphObjectManager) -> None:
    gom.indexes.drop(BY_REPO)
    gom.constraints.drop(UNIQUE_CODE)
    OracleSeed(gom.persistence_manager, "OraIssue").clear()
    OracleSeed(gom.persistence_manager, "OraComment").clear()


@pytest.fixture(scope="module")
def issues(factory: PersistenceManagerFactory) -> Iterator[None]:
    pm = factory.get("oracle")
    gom = StatelessGraphObjectManager(pm)
    tidy(gom)
    # Issues over 50 repos, 400 each, 20 of each repo's closed; comments of the same repos under another label.
    pm.execute(
        QuerySpecification[Any](
            "INSERT INTO drivine_node (labels, props) SELECT JSON('[\"OraIssue\"]'), JSON_OBJECT('id' VALUE 'i' || "
            "LEVEL, 'repoKey' VALUE 'r' || MOD(LEVEL, 50), 'code' VALUE 'c' || LEVEL, 'state' VALUE CASE "
            "WHEN MOD(TRUNC(LEVEL / 50), 20) = 0 THEN 'closed' ELSE 'open' END RETURNING JSON) FROM dual CONNECT BY "
            f"LEVEL <= {ISSUES}"
        )
    )
    pm.execute(
        QuerySpecification[Any](
            "INSERT INTO drivine_node (labels, props) SELECT JSON('[\"OraComment\"]'), "
            "JSON_OBJECT('id' VALUE 'x' || LEVEL, 'repoKey' VALUE 'r' || MOD(LEVEL, 50) RETURNING JSON) "
            "FROM dual CONNECT BY LEVEL <= 10"
        )
    )
    # A repo key stored as a number, beside one stored as text.
    OracleSeed(pm, "OraIssue").node([], {"id": "n7", "repoKey": 7, "state": "open"})
    OracleSeed(pm, "OraIssue").node([], {"id": "t7", "repoKey": "7", "state": "open"})
    pm.execute(QuerySpecification[Any]("BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, 'DRIVINE_NODE'); END;"))
    yield
    tidy(gom)


pytestmark = [ON_ORACLE, pytest.mark.usefixtures("issues")]


def ids(gom: StatelessGraphObjectManager, where: Where) -> list[str]:
    """The ids a load with the condition finds, in order."""
    return sorted(it.id for it in gom.load_all(Issue, where=where))


def plan_of(gom: StatelessGraphObjectManager, pm: PersistenceManager, where: Where) -> list[str]:
    """The objects the plan of the load with the condition reads: tables and indexes."""
    model = gom.registry.fragment(Issue)
    rendered = gom.backend.render_query(
        Query(where=where), model, lambda it: gom.registry.fragment(it).labels, gom.index_advisor.indexed()
    )
    statement = gom.backend.load_all_fragments(model, rendered.where, Page(), rendered.labels_tested)
    pm.execute(
        QuerySpecification[Any](f"EXPLAIN PLAN SET STATEMENT_ID = 'drivine65' FOR {statement.text}").bind(
            dict(rendered.bindings)
        )
    )
    rows = pm.query(
        QuerySpecification[str](
            'SELECT object_name AS "name" FROM plan_table '
            "WHERE statement_id = 'drivine65' AND object_name IS NOT NULL"
        )
    )
    pm.execute(QuerySpecification[Any]("DELETE FROM plan_table WHERE statement_id = 'drivine65'"))
    return rows


def test_finds_through_the_index_what_it_finds_without_it(gom: StatelessGraphObjectManager):
    gom.indexes.drop(BY_REPO)
    conditions: list[Where] = [
        [q.repo_key.eq("r7"), q.state.eq("closed")],
        [q.repo_key.gte("r48"), q.repo_key.lt("r49")],
        q.repo_key.eq("7"),
        any_of(q.repo_key.eq("r1"), q.repo_key.eq("r2")),
    ]
    without = [ids(gom, it) for it in conditions]
    gom.indexes.ensure(BY_REPO)
    assert [ids(gom, it) for it in conditions] == without
    assert len(without[0]) == ISSUES // 50 // 20
    assert gom.count(Issue, q.repo_key.eq("r7")) == ISSUES // 50
    v = refs(IssueView)
    views = gom.load_all(IssueView, where=[v.issue.repo_key.eq("r7"), v.issue.state.eq("closed")])
    assert sorted(it.issue.id for it in views) == without[0]


def test_reads_the_index_and_not_the_label_for_a_comparison_every_root_meets(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    gom.indexes.ensure(BY_REPO)
    indexed = plan_of(gom, pm, [q.repo_key.eq("r7"), q.state.eq("closed")])
    assert any(it.startswith("DRIVINE_R_") for it in indexed)
    assert "DRIVINE_NODE_LABELS" not in indexed
    # Inside an or, the label is read as before.
    either = plan_of(gom, pm, any_of(q.repo_key.eq("r1"), q.repo_key.eq("r2")))
    assert not any(it.startswith("DRIVINE_R_") for it in either)


def test_reads_a_uniqueness_constraint_over_the_property_as_it_reads_a_range_index(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    gom.constraints.ensure(UNIQUE_CODE)
    plan = plan_of(gom, pm, q.code.eq("c42"))
    assert any(it.startswith("DRIVINE_U_") for it in plan)
    assert "DRIVINE_NODE_LABELS" not in plan
    assert ids(gom, q.code.eq("c42")) == ["i42"]


def test_stops_reading_through_an_index_this_manager_drops(gom: StatelessGraphObjectManager):
    gom.indexes.ensure(BY_REPO)

    def tested() -> tuple[str, ...]:
        model = gom.registry.fragment(Issue)
        indexed = gom.index_advisor.indexed()
        rendered = gom.backend.render_query(
            Query(where=q.repo_key.eq("r7")), model, lambda it: gom.registry.fragment(it).labels, indexed
        )
        return rendered.labels_tested

    assert tested() == ("OraIssue",)
    gom.indexes.drop(BY_REPO)
    assert tested() == ()
    assert len(ids(gom, [q.repo_key.eq("r7"), q.state.eq("closed")])) == ISSUES // 50 // 20


def test_deletes_what_a_load_finds(gom: StatelessGraphObjectManager):
    gom.indexes.ensure(BY_REPO)
    closed: Where = [q.repo_key.eq("r9"), q.state.eq("closed")]
    found = ids(gom, closed)
    assert gom.delete_all(Issue, closed) == len(found)
    assert ids(gom, closed) == []
