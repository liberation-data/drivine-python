"""
The shared module's schema planner for one store's graph (its `schemaPlan` request, drivine-cypher-spec#107, #135):
which indexes and constraints to make or drop, and what to record in the schema marker, decided by the module a round at
a time, while a host runs each round's statements on its own connections and gives back what each did, as drivine4j's
`SchemaPlanDriver` and Drivine for TypeScript's `SchemaPlan` run it. plans.yaml pins every decision.

This module is what the planner is asked, in its own terms, and what its answers are in drivine-python's: the specs,
items and results, a failure's chain of errors as the module's rules read it, and the plan's requests. Running a plan's
rounds is schema_plan_driver's, written for the async API and the sync one alike.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, cast

from drivine.backend.store_statements import ModuleStore
from drivine.drivine_error import DrivineError
from drivine.schema.engine_vector_options import EngineVectorOptions
from drivine.schema.ensure_result import EnsureResult, EnsureStatus, ViolatingKey
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_item import SchemaItem
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec

PlanSpec = RangeIndexSpec | UniquenessConstraintSpec | VectorIndexSpec | FullTextIndexSpec
"""An index or a constraint, as drivine-python declares one."""

Call = Literal["ENSURE", "RECREATE", "DROP"]
"""One of the public index and constraint calls, as the module plans it."""

KINDS: dict[str, str] = {
    "RANGE": "RANGE_INDEX",
    "UNIQUE": "UNIQUENESS_CONSTRAINT",
    "VECTOR": "VECTOR_INDEX",
    "FULLTEXT": "FULLTEXT_INDEX",
}
"""The module's name of each kind of item, by drivine-python's."""

SPEC_KINDS: dict[str, str] = {name: kind for kind, name in KINDS.items()}
"""drivine-python's name of each kind of item, by the module's."""

DEFAULT_BUILD_TIMEOUT = 60_000
"""How long, in milliseconds, an index is waited on to be built, unless a manager is told otherwise: the planner's."""

DEFAULT_VIOLATION_SAMPLE = 10
"""How many keys held more than once a violation reports, unless a manager is told otherwise: the planner's."""

_STATUSES: dict[str, EnsureStatus] = {
    "created": "CREATED",
    "alreadyMatching": "ALREADY_MATCHING",
    "drift": "DRIFT",
    "recreated": "RECREATED",
    "violation": "VIOLATION",
    "blockedByIndex": "BLOCKED_BY_INDEX",
}


def module_spec(spec: PlanSpec) -> dict[str, Any]:
    """`spec` as the module reads a spec (`PlanJson.spec`): the module makes it, over lowercased copies where it ignores
    case."""
    written: dict[str, Any] = {
        "kind": KINDS[spec.kind],
        "label": spec.label,
        "properties": list(spec.properties),
        "name": getattr(spec, "name", None),
    }
    if isinstance(spec, RangeIndexSpec):
        written["ignoreCase"] = spec.ignore_case
    elif isinstance(spec, UniquenessConstraintSpec):
        written["ignoreCase"] = spec.ignore_case
        written["nodeIdProperty"] = spec.node_id_property
    elif isinstance(spec, FullTextIndexSpec):
        written["analyzer"] = None
    else:
        written.update(
            dimensions=spec.dimensions,
            similarity=spec.similarity,
            hnswM=spec.hnsw_m,
            hnswEfConstruction=spec.hnsw_ef_construction,
            engineOptions=[_engine_options(it) for it in spec.engine_options],
        )
    return written


def _engine_options(options: EngineVectorOptions) -> dict[str, Any]:
    if options.engine == "Neo4j":
        return {
            "engine": "Neo4j",
            "quantizationEnabled": options.quantization_enabled,
            "hnswM": options.hnsw_m,
            "hnswEfConstruction": options.hnsw_ef_construction,
        }
    return {
        "engine": "FalkorDB",
        "efRuntime": options.ef_runtime,
        "hnswM": options.hnsw_m,
        "hnswEfConstruction": options.hnsw_ef_construction,
    }


def plan_spec(written: Mapping[str, Any]) -> PlanSpec:
    """A spec as the module writes one (`PlanJson.spec`), as drivine-python declares it."""
    kind = SPEC_KINDS.get(written["kind"])
    label: str = written["label"]
    properties = tuple(cast(Sequence[str], written["properties"]))
    name: str | None = written.get("name") or None
    ignore_case = bool(written.get("ignoreCase"))
    if kind == "RANGE":
        return RangeIndexSpec(label, properties, ignore_case=ignore_case, name=name)
    if kind == "UNIQUE":
        node_id = written.get("nodeIdProperty") or None
        return UniquenessConstraintSpec(label, properties, node_id_property=node_id, ignore_case=ignore_case, name=name)
    if kind == "FULLTEXT":
        return FullTextIndexSpec(label, properties)
    if kind == "VECTOR":
        options = cast(Sequence[Mapping[str, Any]], written.get("engineOptions") or [])
        return VectorIndexSpec(
            label,
            (properties[0],),
            written["dimensions"],
            written["similarity"],
            hnsw_m=written.get("hnswM"),
            hnsw_ef_construction=written.get("hnswEfConstruction"),
            engine_options=tuple(
                EngineVectorOptions(
                    engine=it["engine"],
                    hnsw_m=it.get("hnswM"),
                    hnsw_ef_construction=it.get("hnswEfConstruction"),
                    quantization_enabled=it.get("quantizationEnabled"),
                    ef_runtime=it.get("efRuntime"),
                )
                for it in options
            ),
        )
    raise DrivineError(f"No item of drivine-python's is of the module's kind {written['kind']}.")


def schema_item(written: Mapping[str, Any]) -> SchemaItem:
    """An item as the module writes one (`PlanJson.item`, and `items`'s aliases), as drivine-python lists one."""
    return SchemaItem(
        label=written["label"],
        properties=tuple(written["properties"]),
        name=written.get("name"),
        dimensions=written.get("dimensions"),
        similarity=written.get("similarity"),
        hnsw_m=written.get("hnswM"),
        hnsw_ef_construction=written.get("hnswEfConstruction"),
        quantization_enabled=written.get("quantizationEnabled"),
        aliases=tuple(written.get("aliases") or ()),
    )


def ensure_result(written: Mapping[str, Any], spec: Any) -> EnsureResult[Any]:
    """What ensuring an item gave, as the module writes it (keyed by its kind), as drivine-python reports it."""
    kind, value = next(iter(written.items()))
    status = _STATUSES.get(kind)
    if status is None:
        raise DrivineError(f"The schema plan gave a result drivine-python doesn't know: {kind}.")
    given: Mapping[str, Any] = value or {}
    found = schema_item(given["existing"]) if status == "DRIFT" and given.get("existing") else None
    sample = tuple(cast(Sequence[ViolatingKey], given.get("sample") or ())) if status == "VIOLATION" else None
    index = schema_item(given["index"]) if status == "BLOCKED_BY_INDEX" and given.get("index") else None
    return EnsureResult(status, spec, sample=sample, found=found, index=index)


def causes(failure: BaseException) -> list[dict[str, Any]]:
    """
    The chain of `failure`, outermost first, as the module's error rules read it (`Cause`): a SQL driver's error by its
    SQL state and its number (psycopg's `sqlstate`, python-oracledb's `full_code`'s, apsw's `result`), a Neo4j driver's
    (which Memgraph's errors are too) by its code and GQL status, and any other by its message alone.
    """
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    it: BaseException | None = failure
    while it is not None and len(chain) < 10 and id(it) not in seen:
        seen.add(id(it))
        chain.append(_cause(it))
        it = it.__cause__ or it.__context__
    return chain


def _cause(it: BaseException) -> dict[str, Any]:
    cause: dict[str, Any] = {"sqlState": None, "vendorCode": None, "code": None, "gqlStatus": None, "message": str(it)}
    sqlstate = getattr(it, "sqlstate", None)
    oracle = it.args[0] if it.args and hasattr(it.args[0], "full_code") else None
    code = getattr(it, "code", None)
    if isinstance(sqlstate, str):
        cause["sqlState"] = sqlstate
        cause["vendorCode"] = 0
    elif oracle is not None:
        # python-oracledb's error, its number from its `ORA-00955` code.
        number = str(getattr(oracle, "full_code", "")).split("-")[-1]
        cause["vendorCode"] = int(number) if number.isdigit() else getattr(oracle, "code", None)
    elif isinstance(getattr(it, "result", None), int):
        # apsw's result code.
        cause["vendorCode"] = it.result  # type: ignore[attr-defined]
    elif isinstance(code, str) and code.startswith("Neo."):
        cause["code"] = code
        status = getattr(it, "gql_status", None)
        cause["gqlStatus"] = status if isinstance(status, str) else None
    return cause


@dataclass(frozen=True)
class ItemPolicy:
    """How an item call is planned: drivine4j's `SchemaPolicy`, with the longest an index is waited on to be built."""

    mode: Literal["FAIL_FAST", "WARN"] = "FAIL_FAST"
    violation_sample_size: int = DEFAULT_VIOLATION_SAMPLE
    replace_blocking_index: bool = False
    build_timeout_millis: int = DEFAULT_BUILD_TIMEOUT


@dataclass(frozen=True)
class Recreate:
    """Whether the plan recreates an item that drifted, and every item at startup: drivine4j's settings of each."""

    on_drift: bool = False
    on_startup: bool = False


def plan_request(
    database: str,
    operation: Mapping[str, Any],
    policy: ItemPolicy = ItemPolicy(),
    catalogs: Sequence[Mapping[str, Any]] = (),
    runtime: Sequence[Mapping[str, Any]] = (),
    recreate: Recreate = Recreate(),
) -> dict[str, Any]:
    """A request of `operation` on the planning's database: what an item call, ensure or remove plans (`PlanJson`)."""
    return {
        "database": database,
        "operation": dict(operation),
        "catalogs": [dict(it) for it in catalogs],
        "runtime": [dict(it) for it in runtime],
        "policy": {
            "mode": policy.mode,
            "recreateOnDrift": recreate.on_drift,
            "recreateOnStartup": recreate.on_startup,
            "violationSampleSize": policy.violation_sample_size,
            "replaceBlockingIndex": policy.replace_blocking_index,
        },
        "buildTimeoutMillis": policy.build_timeout_millis,
    }


DRIVINE4J_SETTINGS = (
    "Resolve them, set drivine.schema.recreate-on-drift=true (destructive), "
    "drivine.schema.replace-blocking-index=true for an index in the way of a constraint, "
    "drivine.schema.index-build-timeout longer for an index still being built, "
    "or set drivine.schema.mode=WARN to continue with warnings."
)
"""What drivine4j's failure says to set, in its Spring settings' names, which the module writes for every library."""

SCHEMA_OPTIONS = (
    "Resolve them, set recreate_on_drift (destructive), replace_blocking_index for an index in the way of a "
    "constraint, index_build_timeout longer for an index still being built, or set mode to WARN to continue with "
    "warnings."
)
"""The same, in the names of SchemaOptions, which is what a Python application sets."""


def with_option_names(text: str) -> str:
    """A plan's failure or log line, naming the options a Python application sets where the module names drivine4j's."""
    return text.replace(DRIVINE4J_SETTINGS, SCHEMA_OPTIONS)


@dataclass
class SchemaPlan:
    """
    The planner of one store's graph, asked through the module's JSON entry: what a plan's requests are validated,
    listed and answered with. `planning` gives a plan's rounds; schema_plan_driver runs them.
    """

    store: ModuleStore
    graph: str | None = None
    _listings: dict[str, Any] | None = field(default=None, repr=False)

    def validate(self, request: Mapping[str, Any]) -> str | None:
        """Why `request` is refused before anything is applied; None where nothing conflicts."""
        refusal: str | None = self.ask({"validate": dict(request)}).get("refusal")
        return refusal

    def runtime_after(
        self, operation: Mapping[str, Any], runtime: Sequence[Mapping[str, Any]], databases: Sequence[str] | None
    ) -> list[dict[str, Any]]:
        """The runtime catalogs after `operation`, as the planner keeps the items `ensure` gives."""
        asked = {
            "operation": dict(operation),
            "runtime": list(runtime),
            "databases": None if databases is None else list(databases),
        }
        found: list[dict[str, Any]] = self.ask({"runtimeAfter": asked})["runtime"]
        return found

    def runtime_on(self, database: str, runtime: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """The runtime catalogs that hold on `database`, as a request's `runtime` takes them."""
        found: list[dict[str, Any]] = self.ask({"runtimeOn": {"database": database, "runtime": list(runtime)}})[
            "catalogs"
        ]
        return found

    def listings(self) -> dict[str, Any]:
        """The statements that list each kind of index the store has, `indexes` by kind, and its `constraints`."""
        if self._listings is None:
            self._listings = self.ask({"listings": {}})
        return self._listings

    def items(
        self, of: Literal["indexes", "constraints"], rows: Sequence[Any], spec: PlanSpec | None = None
    ) -> list[dict[str, Any]]:
        """The items a listing's `rows` hold, each with its aliases and build state; with `spec`, its identity's."""
        asked: dict[str, Any] = {"of": of, "rows": list(rows), **({"spec": module_spec(spec)} if spec else {})}
        found: list[dict[str, Any]] = self.ask({"items": asked})["items"]
        return found

    def aliased(self, kind: str, item: SchemaItem, aliases: Sequence[str]) -> str | None:
        """
        The statement that records `aliases` as all the aliases of `item`, of drivine-python's `kind` (`RANGE`,
        `UNIQUE`, `VECTOR`, `FULLTEXT`); None on a store that keeps no record.
        """
        named = {"kind": KINDS[kind], "label": item.label, "properties": list(item.properties), "name": item.name}
        statement: str | None = self.ask({"aliased": {"item": named, "aliases": list(aliases)}}).get("statement")
        return statement

    def ask(self, question: Mapping[str, Any]) -> dict[str, Any]:
        """The module's answer to a `schemaPlan` question of this store; its error raised."""
        from drivine.cypher.shared_compiler import module_answer

        request = {
            "schemaPlan": {"store": self.store, **({"graph": self.graph} if self.graph is not None else {}), **question}
        }
        answer = module_answer(json.loads(plain(request)), self.store)
        if "error" in answer:
            raise DrivineError(f"The shared module's schema planner can't answer: {answer['error']}")
        return answer


def plain(value: Any) -> str:
    """
    `value` as plain JSON, as the planner reads a row (`PlanJson.plain`): a whole number of any size as its digits, a
    Decimal as its text's number, and a moment as its ISO text, so a listing's values keep what they are.
    """

    def default(it: Any) -> Any:
        if isinstance(it, Decimal):
            return float(it) if it != it.to_integral_value() else int(it)
        isoformat = getattr(it, "isoformat", None)
        if callable(isoformat):
            return isoformat()
        if isinstance(it, (bytes, bytearray)):
            return bytes(it).decode("utf-8", "replace")
        if isinstance(it, (set, frozenset, tuple)):
            return list(cast(Any, it))
        raise TypeError(f"{type(it).__name__} is no value a listing gives")

    return json.dumps(value, default=default, ensure_ascii=False, separators=(",", ":"))
