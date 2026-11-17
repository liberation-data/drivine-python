# Written by scripts/unasync.py from tests/integration/sql/test_patch_all_refused.py:
# change that, and run the script again.
"""
patch_all is all or nothing on a SQL store: every chunk of PATCH_ROWS rows is bound before any runs, and the chunks run
as one batch that rolls back, so a value refused in a later chunk leaves every node as it was
(liberation-data/drivine-python#512, as drivine4j's patchAll runs as one batch). Drivine for TypeScript's
PatchAllRefused.spec, ported.
"""

from __future__ import annotations

import math
from typing import Annotated, Any

import pytest

from drivine.sync import (
    DrivineError,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    StatelessGraphObjectManager,
    cypher_statement,
)
from drivine.sync.manager.stateless_graph_object_manager import PATCH_ROWS


class Patched(NodeFragment, labels=["Patched"]):
    id: Annotated[str, NodeId()]
    reading: float | None = None


def readings(pm: PersistenceManager) -> list[Any]:
    return pm.query(QuerySpecification[Any](cypher_statement("MATCH (n:Patched) RETURN n.reading ORDER BY n.id")))


def test_a_value_refused_in_a_later_chunk_writes_nothing(gom: StatelessGraphObjectManager, pm: PersistenceManager):
    count = PATCH_ROWS + 1
    nodes = [Patched(id=f"p{i:05d}", reading=1.5) for i in range(count)]
    gom.save_all(nodes)
    patched = [Patched(id=it.id, reading=2.5) for it in nodes]
    # The last, in id order, in the second chunk: NaN, which no SQL store's layout holds in a save.
    patched[-1] = Patched(id=nodes[-1].id, reading=math.nan)
    with pytest.raises(DrivineError, match="NaN"):
        gom.patch_all(patched, only=["reading"])
    assert set(readings(pm)) == {1.5}
