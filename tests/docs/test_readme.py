"""The programs of the README and the docs, run as they are written, so they can't drift from the API."""

import asyncio
import os
import re
from pathlib import Path

import pytest

from drivine import DrivineAssembly, QuerySpecification, cypher_statement

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DOCS = ROOT / "docs"


def blocks(text: str) -> list[str]:
    """The Python blocks of the text."""
    return re.findall(r"```python\n(.*?)```", text, re.S)


def samples(heading: str) -> list[str]:
    """The Python blocks of the docs page that holds the section under the heading."""
    (page,) = [it for it in DOCS.glob("[0-9][0-9]-*.md") if it.read_text().startswith(f"## {heading}\n")]
    return blocks(page.read_text())


def run(program: str) -> None:
    exec(program, {"__name__": "readme"})  # noqa: S102


def delete(*uuids: str) -> None:
    async def deleting() -> None:
        async with DrivineAssembly() as drivine:
            await drivine.persistence_manager().execute(
                QuerySpecification(cypher_statement("MATCH (n) WHERE n.uuid IN $uuids DETACH DELETE n")).bind(
                    {"uuids": list(uuids)}
                )
            )

    asyncio.run(deleting())


def test_the_readme_quick_start_runs_as_written(capsys):
    (program,) = blocks(README.read_text())
    try:
        delete("p1")
        run(program)
        out = capsys.readouterr().out
        assert out.startswith("[Person(") and "name='Ada'" in out
    finally:
        delete("p1")


def test_the_quick_start_runs_as_written(capsys):
    # The models, the program, then the program without the assembly, which is a fragment.
    models, program, _ = samples("Quick start")
    try:
        run(models + program)
        assert capsys.readouterr().out == "Ada ['Studio']\n"
    finally:
        delete("p1", "o1")


@pytest.fixture
def on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """Points DATABASE_* at the tests' Postgres, as the README's environment for the sample does."""
    for name in [it for it in os.environ if it.startswith("DATABASE_")]:
        monkeypatch.delenv(name)
    for name, value in os.environ.copy().items():
        if name.startswith("POSTGRES_DATABASE_"):
            monkeypatch.setenv(name.removeprefix("POSTGRES_"), value)


def test_cypher_on_a_relational_database_runs_as_written(on_postgres: None, capsys):
    (program,) = samples("Cypher on a relational database")
    try:
        delete("p1", "p2")
        run(program)
        assert capsys.readouterr().out == "[{'name': 'Grace', 'since': 2024}]\n"
    finally:
        delete("p1", "p2")
