import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from drivine import DatabaseType, IndexAdvicePolicy, QuerySpecification, SchemaOptions, StatelessGraphObjectManager
from drivine_sample import SampleAssembly

# The datasources the sample runs on, as the main suite's: Drivine's plugin starts a container for each and points these
# at it. The host resolves nowhere, so that with the plugin off the tests fail rather than clear a database of yours.
# Oracle runs only with ORACLE_TESTS=true.
os.environ.setdefault("DATABASE_TYPE", "NEO4J")
os.environ.setdefault("DATABASE_HOST", "test-container.invalid")
os.environ.setdefault("DATABASE_USER", "neo4j")
os.environ.setdefault("MEMGRAPH_DATABASE_TYPE", "MEMGRAPH")
os.environ.setdefault("MEMGRAPH_DATABASE_HOST", "test-container.invalid")
os.environ.setdefault("FALKORDB_DATABASE_TYPE", "FALKORDB")
os.environ.setdefault("FALKORDB_DATABASE_HOST", "test-container.invalid")
if os.environ.get("ORACLE_TESTS") == "true":
    os.environ.setdefault("ORACLE_DATABASE_TYPE", "ORACLE")
    os.environ.setdefault("ORACLE_DATABASE_HOST", "test-container.invalid")
    os.environ.setdefault("ORACLE_DATABASE_NAME", "FREEPDB1")

ENGINES = [
    "default",
    "memgraph",
    "falkordb",
    *(["oracle"] if os.environ.get("ORACLE_DATABASE_TYPE") == "ORACLE" else []),
]
"""The databases each test runs on, by registered name: Neo4j, Memgraph and FalkorDB, and Oracle when it is set."""

SAMPLE_LABELS = [
    "Person",
    "Organization",
    "Issue",
    "GuideUser",
    "WebUser",
    "ContentElement",
    "OddlyStored",
    "SampleResolver",
    "StampedNote",
    "SampleArticle",
    "SampleDoc",
]
"""Every label the sample's classes write, which a test clears before it seeds."""

Start = Callable[..., Awaitable[SampleAssembly]]


async def clear(assembly: SampleAssembly) -> None:
    """Deletes every node of the sample's labels, with its relationships, in the store's own language."""
    pm = assembly.sample_persistence_manager()
    if pm.type is DatabaseType.ORACLE:
        labels = " OR ".join(f"JSON_EXISTS(n.labels, '$?(@ == \"{it}\")')" for it in SAMPLE_LABELS)
        await pm.execute(QuerySpecification[None](f"DELETE FROM drivine_node n WHERE {labels}"))
    else:
        labels = " OR ".join(f"n:{it}" for it in SAMPLE_LABELS)
        await pm.execute(QuerySpecification[None](f"MATCH (n) WHERE {labels} DETACH DELETE n"))


@pytest.fixture(params=ENGINES)
def engine(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
async def start(engine: str) -> AsyncIterator[Start]:
    """Starts the sample over the test's engine, with any schema or index advice, as an application would."""
    started: list[SampleAssembly] = []

    async def starting(
        schema: SchemaOptions | None = None, index_advice: IndexAdvicePolicy = IndexAdvicePolicy.WARN
    ) -> SampleAssembly:
        assembly = SampleAssembly(engine, schema=schema, index_advice=index_advice)
        started.append(assembly)
        await assembly.start()
        await clear(assembly)
        return assembly

    yield starting
    for it in started:
        await clear(it)
        await it.close()


@pytest.fixture
async def sample(start: Start) -> SampleAssembly:
    return await start()


@pytest.fixture
def gom(sample: SampleAssembly) -> StatelessGraphObjectManager:
    return sample.sample_graph_object_manager()
