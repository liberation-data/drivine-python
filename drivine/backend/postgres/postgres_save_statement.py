from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from drivine.backend.postgres.found_stamp import stamps_by_place
from drivine.backend.postgres.postgres_statement import value_of
from drivine.backend.save_statement import SaveStatement
from drivine.model.fragment_model import FragmentModel
from drivine.query.query_specification import QuerySpecification


@dataclass(frozen=True)
class PostgresSaveStatement(SaveStatement):
    """
    A save on Postgres: a call of drivine4j's write function, whose one row is the root's stamp as handed back, then
    `index/found=stamp` for each stamped related node. It is read as the manager reads a save's result, `{found, now,
    related}`: the root found as the stamp the object carried and now as the stamp handed back, so the manager hands
    back that stamp; and each related node's stamp before and after, from which the manager hands back its own.
    """

    handed: Mapping[str, tuple[int, Any]] = field(default_factory=lambda: {})
    """For each index a related node's step was given, the stamped model it is of, by its place among
    `stamped_models`, and its id."""

    @staticmethod
    def made(
        text: str,
        bindings: Mapping[str, Any],
        root: FragmentModel,
        root_object: Any,
        expected_stamp: str | None,
        stamped_models: list[FragmentModel],
        handed: Mapping[str, tuple[int, Any]],
    ) -> PostgresSaveStatement:
        return PostgresSaveStatement(
            text,
            dict(bindings),
            root,
            root_object,
            expected_stamp,
            stamped_models,
            bindings["root"]["id"],
            handed,
        )

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        return super().spec(more).map(lambda row: self._result(value_of(row)))

    def _result(self, row: str) -> dict[str, Any]:
        stamp, *entries = row.split(",")
        return {
            "found": self.expected_stamp or "",
            "now": stamp,
            "related": stamps_by_place(entries, self.handed, len(self.stamped_models)),
        }
