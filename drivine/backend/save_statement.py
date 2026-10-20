from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.model.fragment_model import FragmentModel


@dataclass(frozen=True)
class SaveStatement(GraphStatement):
    """
    A save statement, and what the manager needs to read its result: whether the root was checked against a stamp,
    and which stamp. The statement returns one row, the map `{found, now, related}`: the root's stamp before and
    after, and for each of `stamped_models` the `{id, stamp}` of its nodes before and after. A checked save that found
    the root changed or gone returns none, having written nothing.
    """

    root: FragmentModel | None = None
    root_object: Any = None
    expected_stamp: str | None = None
    stamped_models: list[FragmentModel] = field(default_factory=lambda: [])
    root_id: Any = None

    @staticmethod
    def of(
        text: str,
        bindings: Mapping[str, Any],
        root: FragmentModel,
        root_object: Any,
        expected_stamp: str | None,
        stamped_models: list[FragmentModel],
    ) -> SaveStatement:
        return SaveStatement(
            text, dict(bindings), root, root_object, expected_stamp, stamped_models, bindings["root"]["id"]
        )
