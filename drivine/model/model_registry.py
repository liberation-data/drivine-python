from __future__ import annotations

import threading
from typing import Any, Literal

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from drivine.annotation.aggregate import Aggregate
from drivine.annotation.aggregate_function import AggregateFunction
from drivine.annotation.empty_when_absent import EmptyWhenAbsent
from drivine.annotation.full_text_index import FullTextIndex
from drivine.annotation.graph_path import GraphPath
from drivine.annotation.graph_property import GraphProperty
from drivine.annotation.json_packed import JsonPacked
from drivine.annotation.node_id import NodeId
from drivine.annotation.node_labels import NodeLabels
from drivine.annotation.node_stamp import NodeStamp
from drivine.annotation.null_as_default import NullAsDefault
from drivine.annotation.property_bag import PropertyBag
from drivine.annotation.range_index import RangeIndex
from drivine.annotation.read_only import ReadOnly
from drivine.annotation.relationship import Relationship
from drivine.annotation.root import Root
from drivine.annotation.sorted_by import SortedBy
from drivine.annotation.target_node import TargetNode
from drivine.annotation.transient import Transient
from drivine.annotation.unique import Unique
from drivine.annotation.vector_index import VectorIndex
from drivine.drivine_error import DrivineError
from drivine.model.aggregate_model import AggregateModel
from drivine.model.field_mapping import FieldMapping
from drivine.model.field_shape import FieldShape
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view import GraphView
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.node_fragment import NodeFragment
from drivine.model.node_labels_model import NodeLabelsModel
from drivine.model.path_model import PathModel
from drivine.model.property_bag_model import PropertyBagModel
from drivine.model.property_names import PropertyNames, graph_name
from drivine.model.relationship_fragment import RelationshipFragment
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.model.relationship_model import RelationshipModel
from drivine.model.schema_declaration import SchemaDeclaration
from drivine.model.target_model import RelatedModel, TargetModel
from drivine.model.vector_field import VectorField
from drivine.schema import vector_annotation_check

ClassKind = Literal["NODE_FRAGMENT", "RELATIONSHIP_FRAGMENT", "GRAPH_VIEW"]

MARKERS: tuple[type, ...] = (
    Root,
    NodeId,
    NodeStamp,
    Relationship,
    GraphProperty,
    Transient,
    TargetNode,
    ReadOnly,
    Aggregate,
    GraphPath,
    JsonPacked,
    NullAsDefault,
    EmptyWhenAbsent,
    SortedBy,
    PropertyBag,
    NodeLabels,
    RangeIndex,
    Unique,
    FullTextIndex,
    VectorIndex,
)

_MAPPING = (GraphProperty, Transient, JsonPacked, NullAsDefault, EmptyWhenAbsent, RangeIndex, Unique, FullTextIndex)
"""Markers that shape how a plain field is stored, allowed on node and relationship fragments."""

STAMP_PROPERTY = "__drivine.stamp"
RESERVED_PREFIX = "__drivine."


class ModelRegistry:
    """
    Builds the model of each mapped class from its fields and markers, once, and checks it. Models are built when first
    asked for, by which time every class a field refers to has been declared.
    """

    def __init__(self, property_names: PropertyNames = "camel") -> None:
        self.property_names = property_names
        """How fields are named in the store, for classes that don't say."""
        self._fragments: dict[type, FragmentModel] = {}
        self._relationship_fragments: dict[type, RelationshipFragmentModel] = {}
        self._views: dict[type, GraphViewModel] = {}
        self._registered_subtypes: dict[type, list[type]] = {}
        self._thread = threading.local()
        """
        The thread's `building`: what has been cached since the outermost view it is building was asked for, each
        model's cache and class. A view that fails takes all of them with it, as one built on its account may refer to
        it. Kept per thread, as the sync API may share the registry between threads, each building its own view.
        """

    # ----- Kinds and subtypes -----

    @staticmethod
    def kind_of(type_: Any) -> ClassKind | None:
        if not isinstance(type_, type):
            return None
        if issubclass(type_, NodeFragment):
            return "NODE_FRAGMENT"
        if issubclass(type_, RelationshipFragment):
            return "RELATIONSHIP_FRAGMENT"
        if issubclass(type_, GraphView):
            return "GRAPH_VIEW"
        return None

    def register_subtype(self, parent: type, subtype: type) -> None:
        """
        Registers `subtype` as a kind of `parent` that it doesn't extend: a node of `parent`'s labels that also
        carries `subtype`'s is loaded as a `subtype`. A subclass needs no registering.
        """
        if self.kind_of(subtype) != "NODE_FRAGMENT":
            raise DrivineError(
                f"{subtype.__name__}, registered as a subtype of {parent.__name__}, isn't a NodeFragment."
            )
        self._registered_subtypes.setdefault(parent, []).append(subtype)

    def subtypes(self, type_: type) -> list[type]:
        """The known subtypes of a fragment class: its subclasses that have been imported, and those registered."""
        found: list[type] = []
        pending = list(type_.__subclasses__()) + self._registered_subtypes.get(type_, [])
        while pending:
            it = pending.pop()
            if it not in found and self.kind_of(it) == "NODE_FRAGMENT":
                found.append(it)
                pending.extend(it.__subclasses__())
        return found

    def fragment_for(self, model: FragmentModel, labels: list[str]) -> FragmentModel:
        """
        The model to load a node of `model` with, given the node's labels: the most specific of `model` and its
        subtypes whose labels the node carries all of.
        """
        best = model
        for subtype in self.subtypes(model.type):
            candidate = self.fragment(subtype)
            if len(candidate.labels) > len(best.labels) and all(it in labels for it in candidate.labels):
                best = candidate
        return best

    # ----- Models -----

    def model_of(self, type_: Any) -> TargetModel:
        """The model of a class a statement can load: a node fragment, or a view."""
        kind = self.kind_of(type_)
        if kind == "NODE_FRAGMENT":
            return self.fragment(type_)
        if kind == "GRAPH_VIEW":
            return self.view(type_)
        name = getattr(type_, "__name__", repr(type_))
        if kind == "RELATIONSHIP_FRAGMENT":
            raise DrivineError(f"{name} is a RelationshipFragment, which is loaded as part of a view, not on its own.")
        raise DrivineError(f"{name} is not a NodeFragment or a GraphView.")

    def fragment(self, type_: type) -> FragmentModel:
        model = self._fragments.get(type_)
        if model is None:
            model = self._build_fragment(type_)
            self._fragments[type_] = model
        return model

    def relationship_fragment(self, type_: type) -> RelationshipFragmentModel:
        model = self._relationship_fragments.get(type_)
        if model is None:
            model = self._build_relationship_fragment(type_)
            self._relationship_fragments[type_] = model
            # It may lead to a view still being built, so it goes if that view fails.
            building: list[tuple[dict[type, Any], type]] | None = getattr(self._thread, "building", None)
            if building is not None:
                building.append((self._relationship_fragments, type_))
        return model

    def view(self, type_: type) -> GraphViewModel:
        model = self._views.get(type_)
        if model is not None:
            return model
        building: list[tuple[dict[type, Any], type]] | None = getattr(self._thread, "building", None)
        outermost = building is None
        if building is None:
            building = self._thread.building = []
        try:
            # Cached before its relationships are resolved, so that a view that refers to itself finds itself.
            model = self._build_view(type_)
            self._views[type_] = model
            building.append((self._views, type_))
            for it in model.relationships:
                _ = it.related
            for it in model.paths:
                _ = it.target
        except BaseException:
            # Everything cached while this was built may refer to it: a view that leads back to it, as two views that
            # refer to each other do. Each is built again, and refused, when it is next asked for.
            if outermost:
                for cache, built in building:
                    cache.pop(built, None)
            raise
        finally:
            if outermost:
                self._thread.building = None
        return model

    # ----- Building -----

    def _build_fragment(self, type_: type) -> FragmentModel:
        self._assert_kind(type_, "NODE_FRAGMENT", "NodeFragment")
        labels: list[str] = []
        for it in type_.__mro__:
            for label in it.__dict__.get("__drivine_labels__", ()):
                if label not in labels:
                    labels.append(label)
        if not labels:
            raise DrivineError(
                f"NodeFragment {type_.__name__} has no labels. Give it at least one: "
                f'class {type_.__name__}(NodeFragment, labels=["{type_.__name__}"]).'
            )
        fields = self._fields(type_)
        self._assert_only(
            type_, fields, (NodeId, NodeStamp, PropertyBag, NodeLabels, VectorIndex, *_MAPPING), "a NodeFragment"
        )
        ids = [name for name, (_, marks) in fields.items() if NodeId in marks]
        if len(ids) > 1:
            raise DrivineError(f"{type_.__name__} has {len(ids)} NodeId fields ({', '.join(ids)}). A node has one id.")
        stamps = [name for name, (_, marks) in fields.items() if NodeStamp in marks]
        if len(stamps) > 1:
            raise DrivineError(
                f"{type_.__name__} has {len(stamps)} NodeStamp fields ({', '.join(stamps)}). A node has one stamp."
            )
        stamp = stamps[0] if stamps else None
        if stamp is not None and (fields[stamp][1].keys() - {NodeStamp}):
            raise DrivineError(f"NodeStamp {type_.__name__}.{stamp} can't carry another marker.")
        naming = self._naming(type_)
        node_labels = self._node_labels(type_, fields, labels)
        bags = self._property_bags(type_, fields, naming)
        apart = {stamp, node_labels.field if node_labels else None, *(it.field for it in bags)}
        mapping = self._field_mapping(type_, {k: v for k, v in fields.items() if k not in apart}, naming)
        id_field = ids[0] if ids else None
        schema = self._schema(type_, fields, mapping.graph_properties)
        model = FragmentModel(
            graph_properties=mapping.graph_properties,
            transient_fields=mapping.transient_fields,
            json_packed=mapping.json_packed,
            null_as_default=mapping.null_as_default,
            empty_when_absent=mapping.empty_when_absent,
            collections=mapping.collections,
            type=type_,
            labels=labels,
            node_id_field=id_field,
            node_id_property=mapping.graph_properties.get(id_field) if id_field else None,
            stamp_field=stamp,
            node_labels=node_labels,
            property_bags=bags,
            validate_on_load=self._class_setting(type_, "__drivine_validate_on_load__", True),
            vector_fields={
                it.field: VectorField(it.properties[0], it.similarity) for it in schema if it.kind == "VECTOR"
            },
            full_text_indexes=[it.properties for it in schema if it.kind == "FULLTEXT"],
            schema=schema,
        )
        # A catalog's vector index over a field without VectorIndex is refused here as well as when the catalog is
        # applied, since either may come first.
        vector_annotation_check.admit(model)
        return model

    def _build_relationship_fragment(self, type_: type) -> RelationshipFragmentModel:
        self._assert_kind(type_, "RELATIONSHIP_FRAGMENT", "RelationshipFragment")
        fields = self._fields(type_)
        self._assert_only(type_, fields, (TargetNode, *_MAPPING), "a RelationshipFragment")
        targets = [name for name, (_, marks) in fields.items() if TargetNode in marks]
        if len(targets) != 1:
            raise DrivineError(
                f"RelationshipFragment {type_.__name__} needs exactly one TargetNode field, the node the relationship "
                f"leads to. It has {len(targets)}."
            )
        target_field = targets[0]
        shape = FieldShape.of(fields[target_field][0].annotation)
        where = f"{type_.__name__}.{target_field}"
        if shape.collection is not None:
            raise DrivineError(f"{where} holds the one node the relationship leads to, not a collection.")
        target = self._related(shape.item, where)
        if isinstance(target, RelationshipFragmentModel):
            raise DrivineError(f"{where} must hold a node fragment or a view, not a relationship fragment.")
        naming = self._naming(type_)
        mapping = self._field_mapping(type_, {k: v for k, v in fields.items() if k != target_field}, naming)
        return RelationshipFragmentModel(
            graph_properties=mapping.graph_properties,
            transient_fields=mapping.transient_fields,
            json_packed=mapping.json_packed,
            null_as_default=mapping.null_as_default,
            empty_when_absent=mapping.empty_when_absent,
            collections=mapping.collections,
            type=type_,
            target_field=target_field,
            target=target,
        )

    def _build_view(self, type_: type) -> GraphViewModel:
        self._assert_kind(type_, "GRAPH_VIEW", "GraphView")
        fields = self._fields(type_)
        self._assert_only(
            type_, fields, (Root, Relationship, Transient, ReadOnly, Aggregate, GraphPath, SortedBy), "a GraphView"
        )
        naming = self._naming(type_)
        name = type_.__name__
        for field, (info, marks) in fields.items():
            kinds = [it for it in (Root, Relationship, GraphPath, Aggregate) if it in marks]
            if len(kinds) > 1:
                raise DrivineError(f"{name}.{field} can be one of a root, a relationship, a path or an aggregate.")
            if ReadOnly in marks and not ({Relationship, GraphPath, Aggregate} & marks.keys()):
                raise DrivineError(f"ReadOnly {name}.{field} marks a relationship field; it has none.")
            if not kinds and Transient not in marks:
                raise DrivineError(
                    f"{name}.{field} is not a root, relationship, path or aggregate. A view's other fields aren't "
                    f"loaded: mark it Transient(), with a default."
                )
            if Transient in marks and info.is_required():
                raise DrivineError(f"Transient {name}.{field} is never loaded, so it needs a default.")
        roots = [field for field, (_, marks) in fields.items() if Root in marks]
        if len(roots) != 1:
            raise DrivineError(f"GraphView {name} needs exactly one Root field. It has {len(roots)}.")
        root_field = roots[0]
        root_shape = FieldShape.of(fields[root_field][0].annotation)
        if root_shape.collection is not None or self.kind_of(root_shape.item) != "NODE_FRAGMENT":
            raise DrivineError(
                f"The Root of {name}, {root_field}, must be a NodeFragment. "
                f"{getattr(root_shape.item, '__name__', root_shape.item)} is not."
            )
        relationships = [
            self._relationship(type_, field, info, marks, naming)
            for field, (info, marks) in fields.items()
            if Relationship in marks
        ]
        paths = [
            self._path(type_, field, info, marks, naming)
            for field, (info, marks) in fields.items()
            if GraphPath in marks
        ]
        aggregates: list[AggregateModel] = []
        for field, (_, marks) in fields.items():
            aggregate = marks.get(Aggregate)
            if isinstance(aggregate, Aggregate):
                if not aggregate.type:
                    raise DrivineError(f"The aggregate {name}.{field} needs a relationship type.")
                if aggregate.function is not AggregateFunction.COUNT and not aggregate.property:
                    raise DrivineError(f"The {aggregate.function} {name}.{field} needs the property it aggregates.")
                aggregates.append(
                    AggregateModel(
                        field,
                        graph_name(field, naming),
                        aggregate.function,
                        aggregate.type,
                        aggregate.property,
                        aggregate.direction,
                    )
                )
        return GraphViewModel(type_, root_field, self.fragment(root_shape.item), relationships, paths, aggregates)

    def _relationship(
        self, view: type, field: str, info: FieldInfo, marks: dict[type, Any], naming: PropertyNames
    ) -> RelationshipModel:
        options: Relationship = marks[Relationship]
        where = f"{view.__name__}.{field}"
        if not options.type:
            raise DrivineError(f"The Relationship of {where} needs a type.")
        depth = options.max_depth
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise DrivineError(f"max_depth on {where} must be a whole number of at least 1.")
        shape = FieldShape.of(info.annotation)
        if shape.collection == "dict":
            raise DrivineError(f"{where} is a dict. A relationship holds one item, or a list, set or tuple of them.")
        is_collection = shape.collection is not None
        sorted_by = marks.get(SortedBy)
        if sorted_by is not None and not is_collection:
            raise DrivineError(f"SortedBy {where} sorts a list; the field holds one item.")
        item = shape.item

        def resolve() -> RelatedModel:
            related = self._related(item, where)
            if isinstance(related, RelationshipFragmentModel) and options.max_depth > 1:
                raise DrivineError(
                    f"{where} holds relationship fragments, which are one relationship each: max_depth can't be "
                    f"above 1."
                )
            return related

        return RelationshipModel(
            field=field,
            key=graph_name(field, naming),
            type=options.type,
            direction=options.direction,
            max_depth=options.max_depth,
            required=options.required or (not is_collection and not shape.optional),
            is_collection=is_collection,
            marked_read_only=ReadOnly in marks,
            sorted_by=sorted_by,
            resolve=resolve,
            collection=shape.collection,
        )

    def _path(
        self, view: type, field: str, info: FieldInfo, marks: dict[type, Any], naming: PropertyNames
    ) -> PathModel:
        options: GraphPath = marks[GraphPath]
        where = f"{view.__name__}.{field}"
        if not options.hops or any(not it.type for it in options.hops):
            raise DrivineError(f"The GraphPath of {where} needs at least one hop, each with a type.")
        if sum(1 for it in options.hops if it.order_by) > 1:
            raise DrivineError(f"The GraphPath of {where} names an order on more than one hop; a path is read in one.")
        shape = FieldShape.of(info.annotation)
        if shape.collection == "dict":
            raise DrivineError(f"{where} is a dict. A path holds one node, or a list, set or tuple of them.")
        is_collection = shape.collection is not None
        item = shape.item

        def resolve() -> FragmentModel:
            if self.kind_of(item) != "NODE_FRAGMENT":
                raise DrivineError(
                    f"The path {where} must end at a NodeFragment. {getattr(item, '__name__', item)} is not one."
                )
            return self.fragment(item)

        return PathModel(
            field=field,
            key=graph_name(field, naming),
            hops=tuple(options.hops),
            is_collection=is_collection,
            required=options.required or (not is_collection and not shape.optional),
            resolve=resolve,
            collection=shape.collection,
        )

    def _related(self, type_: Any, where: str) -> RelatedModel:
        kind = self.kind_of(type_)
        if kind == "NODE_FRAGMENT":
            return self.fragment(type_)
        if kind == "GRAPH_VIEW":
            return self.view(type_)
        if kind == "RELATIONSHIP_FRAGMENT":
            return self.relationship_fragment(type_)
        raise DrivineError(
            f"{where} holds {getattr(type_, '__name__', type_)}, which is not a NodeFragment, RelationshipFragment or "
            f"GraphView."
        )

    def _field_mapping(
        self, type_: type, fields: dict[str, tuple[FieldInfo, dict[type, Any]]], naming: PropertyNames
    ) -> FieldMapping:
        mapping = FieldMapping()
        stored: dict[str, str] = {}
        for field, (info, marks) in fields.items():
            where = f"{type_.__name__}.{field}"
            if Transient in marks:
                if info.is_required():
                    raise DrivineError(f"Transient {where} is never loaded, so it needs a default.")
                mapping.transient_fields.add(field)
                continue
            renamed = marks.get(GraphProperty)
            name = renamed.name if isinstance(renamed, GraphProperty) else graph_name(field, naming)
            if name.startswith(RESERVED_PREFIX):
                raise DrivineError(
                    f"{where} is stored as {name!r}; properties beginning {RESERVED_PREFIX!r} are reserved."
                )
            if name in stored:
                raise DrivineError(f"{where} and {type_.__name__}.{stored[name]} are both stored as {name!r}.")
            stored[name] = field
            mapping.graph_properties[field] = name
            if JsonPacked in marks:
                mapping.json_packed.add(field)
            if NullAsDefault in marks:
                if info.is_required():
                    raise DrivineError(f"NullAsDefault {where} loads a stored null as its default, so it needs one.")
                mapping.null_as_default.add(field)
            kind = FieldShape.of(info.annotation).collection
            if kind in ("set", "tuple"):
                mapping.collections[field] = kind
            if EmptyWhenAbsent in marks:
                if kind is None:
                    raise DrivineError(f"EmptyWhenAbsent {where} loads an empty collection; the field isn't one.")
                mapping.empty_when_absent[field] = kind
        return mapping

    def _schema(
        self, type_: type, fields: dict[str, tuple[FieldInfo, dict[type, Any]]], stored: dict[str, str]
    ) -> list[SchemaDeclaration]:
        """The schema items a fragment declares on its fields, and as class arguments over the fields they name."""

        def stored_name(field: str) -> str:
            if field not in stored:
                raise DrivineError(f"{type_.__name__} declares a schema item over {field}, which isn't a stored field.")
            return stored[field]

        declarations: list[SchemaDeclaration] = []
        kinds = {RangeIndex: "RANGE", Unique: "UNIQUE", FullTextIndex: "FULLTEXT"}
        for field, (_, marks) in fields.items():
            for marker, kind in kinds.items():
                if marker in marks:
                    declarations.append(SchemaDeclaration(kind, (stored_name(field),), field))  # pyright: ignore[reportArgumentType]
            vector = marks.get(VectorIndex)
            if isinstance(vector, VectorIndex):
                declarations.append(
                    SchemaDeclaration(
                        "VECTOR",
                        (stored_name(field),),
                        field,
                        vector.similarity,
                        vector.dimensions,
                        vector.hnsw_m,
                        vector.hnsw_ef_construction,
                    )
                )
        for it in type_.__mro__:
            declared: tuple[Any, ...] = it.__dict__.get("__drivine_schema__", ())
            for item in declared:
                kind = kinds.get(item.__class__)
                if kind is None:
                    raise DrivineError(
                        f"{type_.__name__}'s schema argument takes RangeIndex, Unique and FullTextIndex; not {item!r}. "
                        f"A VectorIndex goes on its field."
                    )
                if not item.properties:
                    raise DrivineError(
                        f"On a class, a {type(item).__name__} names its fields: {type(item).__name__}([...])."
                    )
                declarations.append(SchemaDeclaration(kind, tuple(stored_name(f) for f in item.properties)))  # pyright: ignore[reportArgumentType]
        return declarations

    def _node_labels(
        self, type_: type, fields: dict[str, tuple[FieldInfo, dict[type, Any]]], own: list[str]
    ) -> NodeLabelsModel | None:
        marked = [(field, info, marks[NodeLabels]) for field, (info, marks) in fields.items() if NodeLabels in marks]
        if len(marked) > 1:
            raise DrivineError(f"{type_.__name__} has {len(marked)} NodeLabels fields; a fragment may have one.")
        if not marked:
            return None
        field, info, options = marked[0]
        where = f"NodeLabels {type_.__name__}.{field}"
        shape = FieldShape.of(info.annotation)
        if shape.collection not in ("list", "set", "tuple"):
            raise DrivineError(f"{where} holds labels: a list or set of str.")
        fixed = [it for it in options.members or () if it in own]
        if fixed:
            raise DrivineError(f"{where} names {', '.join(fixed)}, the fragment's own label: those are fixed.")
        if any(it.startswith(RESERVED_PREFIX) for it in options.members or ()):
            raise DrivineError(f"{where} names a label beginning {RESERVED_PREFIX!r}, which is reserved.")
        return NodeLabelsModel(field, options.members, shape.collection == "set", f"{RESERVED_PREFIX}labels.{field}")

    def _property_bags(
        self, type_: type, fields: dict[str, tuple[FieldInfo, dict[type, Any]]], naming: PropertyNames
    ) -> list[PropertyBagModel]:
        bags: list[PropertyBagModel] = []
        for field, (info, marks) in fields.items():
            options = marks.get(PropertyBag)
            if not isinstance(options, PropertyBag):
                continue
            if FieldShape.of(info.annotation).collection != "dict":
                raise DrivineError(f"PropertyBag {type_.__name__}.{field} holds its entries in a dict.")
            namespace = "" if options.flat else f"{options.prefix or graph_name(field, naming)}{options.delimiter}"
            bags.append(PropertyBagModel(field, namespace, options.flat))
        if sum(1 for it in bags if it.flat) > 1:
            raise DrivineError(f"{type_.__name__} has more than one flat PropertyBag; a fragment may have one.")
        prefixed = [it for it in bags if not it.flat]
        for a in prefixed:
            for b in prefixed:
                if a is not b and b.namespace.startswith(a.namespace):
                    raise DrivineError(
                        f"The PropertyBag namespaces of {type_.__name__}.{a.field} ({a.namespace!r}) and {b.field} "
                        f"({b.namespace!r}) overlap: one would read the other's entries."
                    )
        return bags

    # ----- Reading classes -----

    def _fields(self, type_: type) -> dict[str, tuple[FieldInfo, dict[type, Any]]]:
        """Each field of a Pydantic model, with its Drivine markers by their class."""
        model: type[BaseModel] = type_  # pyright: ignore[reportAssignmentType]
        if not model.__pydantic_complete__:
            try:
                model.model_rebuild()
            except Exception as error:
                raise DrivineError(f"{type_.__name__} refers to a class that isn't defined yet: {error}") from error
        fields: dict[str, tuple[FieldInfo, dict[type, Any]]] = {}
        for name, info in model.model_fields.items():
            marks: dict[type, Any] = {}
            for it in info.metadata:
                if isinstance(it, MARKERS):
                    if type(it) in marks:
                        raise DrivineError(f"{type_.__name__}.{name} has {type(it).__name__} more than once.")
                    marks[type(it)] = it
            fields[name] = (info, marks)
        return fields

    def _naming(self, type_: type) -> PropertyNames:
        return self._class_setting(type_, "__drivine_property_names__", self.property_names)

    @staticmethod
    def _class_setting(type_: type, name: str, default: Any) -> Any:
        """A class argument, from the nearest class in the hierarchy that gave it."""
        for it in type_.__mro__:
            value = it.__dict__.get(name)
            if value is not None:
                return value
        return default

    def _assert_kind(self, type_: type, kind: ClassKind, base: str) -> None:
        if self.kind_of(type_) != kind:
            raise DrivineError(f"{getattr(type_, '__name__', type_)} is not a {base}.")

    @staticmethod
    def _assert_only(
        type_: type, fields: dict[str, tuple[FieldInfo, dict[type, Any]]], allowed: tuple[type, ...], what: str
    ) -> None:
        for field, (_, marks) in fields.items():
            misplaced = [it.__name__ for it in marks if it not in allowed]
            if misplaced:
                raise DrivineError(f"{type_.__name__}.{field}: {', '.join(misplaced)} can't be used on {what}.")
