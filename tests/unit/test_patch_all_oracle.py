"""
patch_all on Oracle finds each object's result by its row's place, not by the id the block gives back: that is Oracle's
plain JSON text of the binary double the node holds, which need not read back as the number given (Drivine for
TypeScript's PatchAllOracle.spec.ts).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, TypeVar

from drivine import DatabaseType, NodeFragment, NodeId, PatchResult, QuerySpecification, StatelessGraphObjectManager
from drivine.backend.sql.sql_json import to_json
from drivine.cursor.cursor import Cursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.manager.persistence_manager import PersistenceManager

T = TypeVar("T")


class Reading(NodeFragment, labels=["Reading"]):
    id: Annotated[float, NodeId()]
    value: int | None = None


class Rounding(PersistenceManager):
    """
    Gives the block's rows in the order of the rows bound, each found and changed at an even place, and each id back as
    a text of fewer digits might read back.
    """

    def __init__(self) -> None:
        self.database = "patch-all-oracle"
        self.type = DatabaseType.ORACLE

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        rows: list[Any] = json.loads(to_json(spec.parameters["rows"]))  # type: ignore[index]
        results: list[Any] = []
        for i, row in enumerate(rows):
            given = row["id"]["$numberDouble"] if isinstance(row["id"], dict) else row["id"]
            results.append({"id": float(f"{float(given):.6g}"), "found": i % 2 == 0, "changed": i % 2 == 0})
        for processor in spec.post_processors:
            results = processor.apply(results)
        return results

    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        raise NotImplementedError


async def test_hands_each_object_its_rows_result_whatever_text_the_block_gives_its_id_back_as():
    ids = [0.1 + 0.2, 1e-7, 123456.789, 4.5]
    gom = StatelessGraphObjectManager(Rounding())
    results = await gom.patch_all([Reading(id=id, value=1) for id in ids], only=["value"])
    # The rows are bound in the order of their ids' texts: 0.30000000000000004, 123456.789, 1e-07, 4.5.
    bound = sorted(ids, key=str)
    expected = [bound.index(id) % 2 == 0 for id in ids]
    assert results == [PatchResult(found, found, found) for found in expected]
