"""The README's programs, run as they are written, so the README can't drift from the API."""

import asyncio
import os
import re
from pathlib import Path

import pytest

from drivine import DrivineAssembly, QuerySpecification, cypher_statement

README = Path(__file__).resolve().parents[2] / "README.md"


def samples(heading: str) -> list[str]:
    """The Python blocks of the README's section under the heading."""
    section = README.read_text().split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"```python\n(.*?)```", section, re.S)


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
