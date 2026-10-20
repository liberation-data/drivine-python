"""The README's quick start, run as it is written, so the README can't drift from the API."""

import re
from pathlib import Path

from drivine import DatabaseRegistry, PersistenceManagerFactory, QuerySpecification

README = Path(__file__).resolve().parents[2] / "README.md"


def quick_start() -> list[str]:
    section = README.read_text().split("## Quick start", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"```python\n(.*?)```", section, re.S)


def test_the_quick_start_runs_as_written(capsys):
    models, program = quick_start()
    namespace: dict[str, object] = {"__name__": "quick_start"}
    exec(models + program, namespace)  # noqa: S102
    assert capsys.readouterr().out == "Ada ['Studio']\n"


def teardown_function() -> None:
    import asyncio

    async def clean() -> None:
        registry = DatabaseRegistry()
        registry.build_or_resolve_from_env()
        pm = PersistenceManagerFactory(registry).get()
        await pm.execute(QuerySpecification("MATCH (n) WHERE n.uuid IN ['p1', 'o1'] DETACH DELETE n"))
        await registry.close()

    asyncio.run(clean())
