"""
A node whose NodeId a SQL store holds as a temporal value's tag, loaded by a model that declares the id a type no tag
loads as, is refused by name (liberation-data/drivine#813, drivine-python#560): the id would load as its text, which a
lookup by the id never finds, so a save of it would make a second node.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any

import pytest

from drivine import DrivineError, GraphProperty, NodeFragment, NodeId
from drivine.backend.sql.value_layout import TagText
from drivine.mapper.graph_object_mapper import GraphObjectMapper
from drivine.model.model_registry import ModelRegistry


class TextId(NodeFragment, labels=["Day"]):
    id: Annotated[str, NodeId()]


class OptionalTextId(NodeFragment, labels=["Day"]):
    id: Annotated[str | None, NodeId()] = None


class NumberId(NodeFragment, labels=["Day"]):
    id: Annotated[int, NodeId()]


class RenamedTextId(NodeFragment, labels=["Day"]):
    key: Annotated[str, NodeId(), GraphProperty("dayKey")]


class Unvalidated(NodeFragment, labels=["Day"], validate_on_load=False):
    id: Annotated[str, NodeId()]


class TextAt(NodeFragment, labels=["Day"]):
    id: Annotated[str, NodeId()]
    at: str


class AnyId(NodeFragment, labels=["Day"]):
    id: Annotated[Any, NodeId()]


class DateId(NodeFragment, labels=["Day"]):
    id: Annotated[date, NodeId()]


class OptionalDateId(NodeFragment, labels=["Day"]):
    id: Annotated[date | None, NodeId()] = None


class DateTimeId(NodeFragment, labels=["Day"]):
    id: Annotated[datetime, NodeId()]


class TimeId(NodeFragment, labels=["Day"]):
    id: Annotated[time, NodeId()]


class DurationId(NodeFragment, labels=["Day"]):
    id: Annotated[timedelta, NodeId()]


def loaded(type_: type[NodeFragment], props: dict[str, Any]) -> Any:
    registry = ModelRegistry()
    model = registry.fragment(type_)
    return GraphObjectMapper(registry).fragment(model, {"props": props, "labels": model.labels})


@pytest.mark.parametrize(
    ("type_", "declared"), [(TextId, "str"), (OptionalTextId, "str | None"), (NumberId, "int"), (Unvalidated, "str")]
)
def test_a_tagged_id_is_refused_by_name_for_a_type_no_tag_loads_as(type_: type[NodeFragment], declared: str):
    message = (
        f"{type_.__name__}'s NodeId id is declared a {declared}, but the node whose id is '2026-01-05' holds it as a"
        " date: declare id as a date to load it."
    )
    with pytest.raises(DrivineError, match=re.escape(message)):
        loaded(type_, {"id": TagText("2026-01-05", "date")})


def test_a_renamed_id_is_read_by_its_stored_name_and_refused_by_its_field():
    refused = re.escape("RenamedTextId's NodeId key is declared a str, but the node whose id is '10:00:00'")
    with pytest.raises(DrivineError, match=refused):
        loaded(RenamedTextId, {"dayKey": TagText("10:00:00", "localtime")})


@pytest.mark.parametrize(
    ("type_", "held"),
    [
        (DateId, TagText("2026-01-05", "date")),
        (OptionalDateId, TagText("2026-01-05", "date")),
        (DateTimeId, TagText("2026-01-05T10:00:00Z", "datetime")),
        (TimeId, TagText("10:00:00", "localtime")),
        (DurationId, TagText("PT1S", "duration")),
        (AnyId, TagText("2026-01-05", "date")),
    ],
)
def test_a_tagged_id_loads_for_a_temporal_type_or_any(type_: type[NodeFragment], held: TagText):
    assert loaded(type_, {"id": held}).id is not None


def test_a_text_id_loads_as_a_plain_str_and_an_untagged_one_is_not_refused():
    assert type(loaded(TextId, {"id": "2026-01-05"}).id) is str
    assert loaded(TextId, {"id": "a"}).id == "a"


def test_a_tagged_property_that_is_no_id_loads_as_a_plain_str_for_a_str_field():
    assert type(loaded(TextAt, {"id": "a", "at": TagText("2026-01-05", "date")}).at) is str
