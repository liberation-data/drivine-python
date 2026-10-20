"""How a query's path of field names is read against the model."""

from __future__ import annotations

from drivine.drivine_error import DrivineError
from drivine.dsl.ref import STORED
from drivine.dsl.scope import Scope
from drivine.dsl.step import Step
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.model.target_model import RelatedModel


def scope_of(related: RelatedModel) -> Scope:
    if isinstance(related, GraphViewModel):
        return Scope("VIEW", related)
    if isinstance(related, FragmentModel):
        return Scope("FRAGMENT", related)
    return Scope("REL_FRAGMENT", related)


def step(scope: Scope, name: str) -> Step:
    if name.startswith(STORED):
        on = "RELATIONSHIP" if scope.kind == "REL_FRAGMENT" else "NODE"
        return Step("PROPERTY", property=name[len(STORED) :], on=on)
    model = scope.model
    if scope.kind == "VIEW":
        assert isinstance(model, GraphViewModel)
        return _view_step(model, name)
    if scope.kind == "FRAGMENT":
        assert isinstance(model, FragmentModel)
        return _fragment_step(model, name)
    if scope.kind == "BAG":
        assert scope.bag is not None
        return Step("PROPERTY", property=f"{scope.bag.namespace}{name}")
    assert isinstance(model, RelationshipFragmentModel)
    return _relationship_fragment_step(model, name)


def _view_step(model: GraphViewModel, name: str) -> Step:
    if name == model.root_field:
        return Step("SAME_NODE", Scope("FRAGMENT", model.root))
    rel = next((it for it in model.relationships if it.field == name), None)
    if rel is not None:
        return Step("HOP", scope_of(rel.related), rel)
    if any(it.field == name for it in model.paths) or any(it.field == name for it in model.aggregates):
        raise DrivineError(
            f"{model.type.__name__}.{name} is computed when the view is read, so a query can't test or order by it yet."
        )
    raise DrivineError(f"{model.type.__name__} has no root or relationship field {name}.")


def _fragment_step(model: FragmentModel, name: str) -> Step:
    if name in model.transient_fields:
        raise DrivineError(f"{model.type.__name__}.{name} is transient: it isn't stored, so a query can't test it.")
    bag = next((it for it in model.property_bags if it.field == name), None)
    if bag is not None:
        return Step("SAME_NODE", Scope("BAG", model, bag))
    if model.node_labels is not None and model.node_labels.field == name:
        return Step("LABELS")
    if name == model.stamp_field:
        # The stamp is stored under the library's property, not the field's name.
        return Step("PROPERTY", property=stamps.PROPERTY)
    stored = model.graph_properties.get(name)
    if stored is None:
        raise DrivineError(f"{model.type.__name__} has no field {name}.")
    return Step("PROPERTY", property=stored, json_packed=name in model.json_packed)


def _relationship_fragment_step(model: RelationshipFragmentModel, name: str) -> Step:
    if name == model.target_field:
        assert model.target is not None
        return Step("TARGET", scope_of(model.target))
    if name in model.transient_fields:
        raise DrivineError(f"{model.type.__name__}.{name} is transient: it isn't stored, so a query can't test it.")
    stored = model.graph_properties.get(name)
    if stored is None:
        raise DrivineError(f"{model.type.__name__} has no field {name}.")
    return Step("PROPERTY", property=stored, on="RELATIONSHIP", json_packed=name in model.json_packed)
