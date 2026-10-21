# Drivine's sample, in Python

The port of drivine4j's sample, as TypeScript's Drivine ports it in `sample/`: an issue tracker and a guide's users,
with fragments, views, relationship fragments, polymorphic users, property bags, stamps and search.

- `drivine_sample/fragment` and `drivine_sample/view` hold the models, a class to a module.
- `SampleAssembly("memgraph")` wires Drivine over one database as an application would: a `DrivineAssembly`, with the
  database's object manager and persistence manager, and the schema and index advice an application configures.
- `tests/` runs drivine4j's sample tests on Neo4j, Memgraph and FalkorDB, and on Oracle with `ORACLE_TESTS=true`: the
  query DSL, grouped reads, patches, label polymorphism, required relationships, deletes, stamps and search.

## Run it

It is a [Poetry](https://python-poetry.org) project, which takes drivine from this repository:

```bash
poetry install         # also writes the typed references: see below
poetry run pytest      # starts Neo4j, Memgraph and FalkorDB in Docker
poetry run pyright     # checks every query against the models
```

## Typed references, written by the build

Queries import `refs` from `drivine_sample/sample_refs.py`, not from drivine:

```python
from drivine_sample.sample_refs import refs

q = refs(RaisedAndAssignedIssue)
await gom.load_all(RaisedAndAssignedIssue, [q.issue.state.eq("open"), q.raised_by.works_for.name.eq("Acme Corp")])
```

`sample_refs.py` is drivine's `refs` at run time, and `sample_refs.pyi` types each field from the models, so pyright and
mypy reject `q.issue.titel` or `q.issue.id.eq("123")`. drivine's stub generator, `python -m drivine.stubs`, writes both.

The build runs it, so the files always match the models. `pyproject.toml` names `build.py` as Poetry's build script,
which `poetry install` and `poetry build` both run:

```toml
[tool.poetry.build]
script = "build.py"
generate-setup-file = false
```

Poetry runs a build script in an isolated environment holding `build-system.requires`. An application that takes drivine
from PyPI lists `drivine` there; this sample reads drivine from the repository instead, so it lists drivine's own
dependencies. The files are committed, so they can be read without a build, and `tests/test_typed_refs_up_to_date.py`
fails when a model changed since they were written.

A build script makes the wheel `poetry build` writes specific to the platform (`cp313-...-macosx_...`), as Poetry treats
a project with one as one with native code: for an application that is deployed, not published, no matter.
