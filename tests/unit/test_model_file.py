"""
The model file the stubs step writes, `.drivine/model.json`: against the format's schema, against what ModelRegistry
builds of the same classes, and as it is kept from one build to the next.
"""

import json
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from drivine.model.model_registry import ModelRegistry
from drivine.stubs.__main__ import main
from drivine.stubs.model_file import FORMAT, ModelReader, Project, model, model_id, text, write_model_file

SCHEMA = json.loads((Path(__file__).parent.parent / "cypher" / "model" / "schema.json").read_text())


def quiet(_: str) -> None:
    return None


def written(project: Project) -> dict:
    return json.loads(project.file.read_text())


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fixture"\n')
    return Project.of(tmp_path)


@pytest.fixture
def modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Writes modules of models into a package of their own, and gives their dotted names."""
    package = f"models_{uuid.uuid4().hex[:8]}"
    (tmp_path / package).mkdir()
    (tmp_path / package / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))

    def write(**sources: str) -> list[str]:
        for name, source in sources.items():
            (tmp_path / package / f"{name}.py").write_text(textwrap.dedent(source))
        for name in list(sys.modules):
            if name.startswith(package):
                del sys.modules[name]
        return [f"{package}.{name}" for name in sources]

    return write


def test_the_file_of_the_test_models_passes_the_format_s_schema(project: Project):
    assert write_model_file(["tests.models"], project, False, quiet) == "written"
    errors = sorted(str(it.message) for it in Draft202012Validator(SCHEMA).iter_errors(written(project)))
    assert errors == []
    assert written(project)["format"] == FORMAT
    assert written(project)["generator"]["library"] == "drivine-python"


def test_each_entry_says_what_model_registry_builds_of_its_class(project: Project):
    """The file must say what a load does: read against the registry's own models of the same classes."""
    write_model_file(["tests.models"], project, False, quiet)
    file = written(project)
    registry = ModelRegistry()
    classes = {model_id(it): it for it in _classes("tests.models")}
    assert file["fragments"], "the test models declare fragments"
    for entry in file["fragments"]:
        runtime = registry.fragment(classes[entry["id"]])
        assert entry["labels"] == runtime.labels
        assert entry.get("nodeId") == runtime.node_id_field
        assert {it["name"]: it["stored"] for it in entry["properties"]} == runtime.graph_properties
        assert {it["name"] for it in entry["properties"] if it.get("packed")} == runtime.json_packed
        assert {
            it["name"] for it in entry["properties"] if it.get("whenAbsent") == "default"
        } == runtime.null_as_default
        assert {it["name"] for it in entry["properties"] if it.get("whenAbsent") == "empty"} == set(
            runtime.empty_when_absent
        )
        assert [(it["field"], it["prefix"]) for it in entry.get("bags", [])] == sorted(
            (it.field, it.namespace) for it in runtime.property_bags
        )
        assert bool(entry.get("stamp")) == (runtime.stamp_field is not None)
        if runtime.node_labels:
            assert entry["labelSet"]["field"] == runtime.node_labels.field
    for entry in file["views"]:
        runtime = registry.view(classes[entry["id"]])
        assert entry["root"] == {"field": runtime.root_field, "fragment": model_id(runtime.root.type)}
        written_rels = {it["field"]: it for it in entry["relationships"]}
        for rel in runtime.relationships:
            it = written_rels[rel.field]
            assert (it["type"], it["direction"], it["target"]) == (
                rel.type,
                str(rel.direction),
                model_id(rel.related.type),
            )
            assert (it["many"], it["required"], it.get("maxDepth", 1)) == (
                rel.is_collection,
                rel.required,
                rel.max_depth,
            )
            assert bool(it.get("readOnly")) == rel.read_only
        for path in runtime.paths:
            it = written_rels[path.field]
            assert it["target"] == model_id(path.target.type)
            assert [hop["type"] for hop in it["path"]] == [hop.type for hop in path.hops]
        assert [it["field"] for it in entry.get("aggregates", [])] == sorted(it.field for it in runtime.aggregates)


def _classes(module: str) -> list[type]:
    import importlib

    found = vars(importlib.import_module(module)).values()
    return [it for it in found if isinstance(it, type) and ModelRegistry.kind_of(it) is not None]


def test_a_field_s_type_and_whether_it_may_be_absent_are_read_from_its_annotation(modules, project: Project):
    names = modules(
        shapes="""
        from __future__ import annotations
        import uuid
        from datetime import date, datetime, time, timedelta
        from decimal import Decimal
        from enum import Enum
        from typing import Annotated, Any, Literal
        from drivine import GraphProperty, JsonPacked, NodeFragment, NodeId

        class Role(str, Enum):
            ADMIN = "admin"
            AUTHOR = "author"

        class Rank(Enum):
            LOW = 1
            HIGH = 2

        class Shape(NodeFragment, labels=["Shape"]):
            id: Annotated[uuid.UUID, NodeId()]
            name: str
            nickname: str | None = None
            age: int
            ratio: float
            price: Decimal
            active: bool
            born: date
            seen: datetime
            at: time
            took: timedelta
            role: Role
            rank: Rank
            status: Literal["open", "closed"]
            tags: list[str] = []
            scores: dict[str, int] = {}
            stored_as: Annotated[str, GraphProperty("weird name")]
            blob: Annotated[dict[str, Any], JsonPacked()] = {}
            anything: Any = None
        """
    )
    write_model_file(names, project, False, quiet)
    properties = {it["name"]: it for it in written(project)["fragments"][0]["properties"]}
    shapes = {name: (it["type"], it.get("of"), it.get("values"), it["nullable"]) for name, it in properties.items()}
    assert shapes == {
        "id": ("uuid", None, None, False),
        "name": ("string", None, None, False),
        "nickname": ("string", None, None, True),
        "age": ("int", None, None, False),
        "ratio": ("float", None, None, False),
        "price": ("float", None, None, False),
        "active": ("boolean", None, None, False),
        "born": ("date", None, None, False),
        "seen": ("datetime", None, None, False),
        "at": ("localTime", None, None, False),
        "took": ("duration", None, None, False),
        "role": ("enum", None, ["admin", "author"], False),
        "rank": ("int", None, None, False),
        "status": ("enum", None, ["closed", "open"], False),
        "tags": ("list", "string", None, False),
        "scores": ("map", "int", None, False),
        "stored_as": ("string", None, None, False),
        "blob": ("map", "any", None, False),
        "anything": ("any", None, None, True),
    }
    assert properties["stored_as"]["stored"] == "weird name"
    assert properties["seen"]["stored"] == "seen"
    assert properties["blob"]["packed"] == "json"


def test_the_file_is_written_once_and_left_alone_while_the_model_is_the_same(project: Project):
    assert write_model_file(["tests.models"], project, True, quiet) == "stale"
    assert write_model_file(["tests.models"], project, False, quiet) == "written"
    before = project.file.stat().st_mtime_ns
    assert write_model_file(["tests.models"], project, False, quiet) == "current"
    assert project.file.stat().st_mtime_ns == before
    assert write_model_file(["tests.models"], project, True, quiet) == "current"


FRAGMENT = """
from typing import Annotated
from drivine import NodeFragment, NodeId

class {name}(NodeFragment, labels=["{name}"]):
    id: Annotated[str, NodeId()]
"""


def test_an_entry_the_code_no_longer_declares_stays_marked_removed_until_it_comes_back(modules, project: Project):
    a, b = modules(a=FRAGMENT.format(name="A"), b=FRAGMENT.format(name="B"))
    write_model_file([a, b], project, False, quiet)
    assert write_model_file([a], project, False, quiet) == "written"
    removed = next(it for it in written(project)["fragments"] if it["name"] == "B")
    assert list(removed)[:3] == ["id", "name", "removed"]
    assert removed["removed"] is True
    assert write_model_file([a], project, False, quiet) == "current"
    write_model_file([a, b], project, False, quiet)
    assert "removed" not in next(it for it in written(project)["fragments"] if it["name"] == "B")


def test_a_project_that_declares_nothing_any_more_keeps_its_file_with_every_entry_removed(modules, project: Project):
    (a,) = modules(a=FRAGMENT.format(name="A"))
    write_model_file([a], project, False, quiet)
    (empty,) = modules(empty="")
    assert write_model_file([empty], project, False, quiet) == "written"
    assert [(it["name"], it["removed"]) for it in written(project)["fragments"]] == [("A", True)]


def test_a_project_that_never_declared_anything_gets_no_file(modules, project: Project):
    (empty,) = modules(empty="")
    assert write_model_file([empty], project, False, quiet) == "empty"
    assert not project.file.exists()


def test_a_file_of_another_format_is_written_over_and_said_so(modules, project: Project):
    (a,) = modules(a=FRAGMENT.format(name="A"))
    project.file.parent.mkdir()
    project.file.write_text('{"format": "drivine-model/9", "fragments": [{"id": "x"}]}')
    warnings: list[str] = []
    write_model_file([a], project, False, warnings.append)
    assert warnings == [f"Drivine model file: {project.file} is not a drivine-model/1 file, so nothing in it is kept."]
    assert [it["name"] for it in written(project)["fragments"]] == ["A"]


def test_a_class_model_registry_refuses_is_named_and_left_out(modules, project: Project):
    (names,) = modules(
        refused="""
        from typing import Annotated
        from drivine import NodeFragment, NodeId

        class Good(NodeFragment, labels=["Good"]):
            id: Annotated[str, NodeId()]

        class Twice(NodeFragment, labels=["Twice"]):
            a: Annotated[str, NodeId()]
            b: Annotated[str, NodeId()]
        """
    )
    warnings: list[str] = []
    write_model_file([names], project, False, warnings.append)
    assert warnings == [f"Drivine model file: {names}.Twice: Twice has 2 NodeId fields (a, b). A node has one id."]
    assert [it["name"] for it in written(project)["fragments"]] == ["Good"]


def test_model_false_under_tool_drivine_turns_it_off(tmp_path: Path, modules):
    (a,) = modules(a=FRAGMENT.format(name="A"))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "off"\n\n[tool.drivine]\nmodel = false\n')
    off = Project.of(tmp_path)
    assert write_model_file([a], off, False, quiet) == "off"
    assert not off.file.exists()


def test_the_project_is_the_nearest_pyproject_and_its_name_poetry_s_too(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[tool.poetry]\nname = "poetic"\n')
    (tmp_path / "pkg" / "deep").mkdir(parents=True)
    found = Project.of(tmp_path / "pkg" / "deep")
    assert (found.directory, found.name, found.enabled) == (tmp_path, "poetic", True)
    assert found.file == tmp_path / ".drivine" / "model.json"


def test_subtypes_are_listed_and_never_said_to_be_all(modules, project: Project):
    (names,) = modules(
        subtypes="""
        from typing import Annotated
        from drivine import NodeFragment, NodeId

        class Animal(NodeFragment, labels=["Animal"]):
            id: Annotated[str, NodeId()]

        class Dog(Animal, labels=["Dog"]):
            pass
        """
    )
    write_model_file([names], project, False, quiet)
    animal = next(it for it in written(project)["fragments"] if it["name"] == "Animal")
    assert animal["subtypes"] == [f"{names}.Dog"]
    # A subtype may be registered in code, which no file can see.
    assert animal["subtypesComplete"] is False


def test_the_stubs_command_writes_the_model_file_and_fails_check_while_it_is_stale(tmp_path: Path, modules):
    (a,) = modules(a=FRAGMENT.format(name="A"))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "cli"\n')
    out = tmp_path / "refs.py"
    assert main([a, "--out", str(out), "--check"]) == 1
    assert main([a, "--out", str(out)]) == 0
    assert (tmp_path / ".drivine" / "model.json").is_file()
    assert main([a, "--out", str(out), "--check"]) == 0
    (tmp_path / ".drivine" / "model.json").write_text("{}\n")
    assert main([a, "--out", str(out), "--check"]) == 1


def test_no_model_writes_none(tmp_path: Path, modules):
    (a,) = modules(a=FRAGMENT.format(name="A"))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "cli"\n')
    assert main([a, "--out", str(tmp_path / "refs.py"), "--no-model"]) == 0
    assert not (tmp_path / ".drivine").exists()


def test_the_text_is_the_same_each_time():
    read = ModelReader(["tests.models"]).read()
    assert text(model("m", read, None)) == text(model("m", ModelReader(["tests.models"]).read(), None))
    assert text(model("m", read, None)).endswith("}\n")


def test_a_module_in_no_project_gets_no_model_file_and_its_stubs_still(tmp_path: Path, modules, capsys):
    (a,) = modules(a=FRAGMENT.format(name="A"))
    out = tmp_path / "elsewhere" / "refs.py"
    out.parent.mkdir()
    # tmp_path holds no pyproject.toml, and nothing above it does either.
    assert main([a, "--out", str(out)]) == 0
    assert out.is_file()
    assert "Drivine model file: not written. There is no pyproject.toml" in capsys.readouterr().err


def test_pydantic_s_checked_dates_and_times_are_the_temporal_types_they_check(modules, project: Project):
    (names,) = modules(
        checked="""
        from typing import Annotated
        from pydantic import AwareDatetime, FutureDate, NaiveDatetime, PastDatetime
        from drivine import NodeFragment, NodeId

        class Event(NodeFragment, labels=["Event"]):
            id: Annotated[str, NodeId()]
            at: AwareDatetime
            local: NaiveDatetime
            happened: PastDatetime
            due: FutureDate | None = None
        """
    )
    write_model_file([names], project, False, quiet)
    properties = {it["name"]: (it["type"], it["nullable"]) for it in written(project)["fragments"][0]["properties"]}
    assert properties == {
        "id": ("string", False),
        "at": ("datetime", False),
        "local": ("localDatetime", False),
        "happened": ("datetime", False),
        "due": ("date", True),
    }


def test_an_index_on_one_property_is_that_property_s_and_one_over_several_the_fragment_s(modules, project: Project):
    (names,) = modules(
        indexed="""
        from typing import Annotated
        from drivine import FullTextIndex, GraphProperty, NodeFragment, NodeId, RangeIndex, Unique, VectorIndex

        class Doc(
            NodeFragment, labels=["Doc"], schema=[Unique(["tenant_id", "user_id"]), RangeIndex(["title"])]
        ):
            id: Annotated[str, NodeId()]
            email: Annotated[str, Unique(), GraphProperty("mail")]
            title: str
            body: Annotated[str, FullTextIndex()]
            tenant_id: str
            user_id: str
            embedding: Annotated[list[float] | None, VectorIndex(similarity="EUCLIDEAN", dimensions=3)] = None
            other: Annotated[list[float] | None, VectorIndex()] = None
        """
    )
    write_model_file([names], project, False, quiet)
    (doc,) = written(project)["fragments"]
    properties = {it["name"]: it for it in doc["properties"]}
    assert {name: it.get("index") for name, it in properties.items() if it.get("index")} == {
        "email": "unique",
        "title": "range",
        "body": "fullText",
    }
    assert properties["embedding"]["vector"] == {"similarity": "EUCLIDEAN", "dimensions": 3}
    assert properties["other"]["vector"] == {"similarity": "COSINE"}
    # Named by field, as the format names a property, though the fields are stored as tenantId and userId.
    assert doc["indexes"] == [{"kind": "unique", "properties": ["tenant_id", "user_id"]}]


def test_a_class_of_another_module_a_view_leads_to_is_written_too(modules, project: Project):
    other, views = modules(
        other=FRAGMENT.format(name="Other"),
        views="""
        from typing import Annotated
        from drivine import GraphView, Relationship, Root
        from .other import Other

        class Pair(GraphView):
            root: Annotated[Other, Root()]
            next: Annotated[Other | None, Relationship("NEXT")] = None
        """,
    )
    write_model_file([views], project, False, quiet)
    file = written(project)
    assert [it["id"] for it in file["fragments"]] == [f"{other}.Other"]
    assert [it["id"] for it in file["views"]] == [f"{views}.Pair"]


def test_check_names_the_stale_model_file_by_its_path(tmp_path: Path, modules, capsys):
    (a,) = modules(a=FRAGMENT.format(name="A"))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "cli"\n')
    out = tmp_path / "refs.py"
    assert main([a, "--out", str(out)]) == 0
    (tmp_path / ".drivine" / "model.json").write_text("{}\n")
    capsys.readouterr()
    assert main([a, "--out", str(out), "--check"]) == 1
    assert f"Out of date: {tmp_path / '.drivine' / 'model.json'}." in capsys.readouterr().err
    assert main([a, "--out", str(out), "--check", "--no-model"]) == 0
