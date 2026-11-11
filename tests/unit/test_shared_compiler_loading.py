"""
The shared compiler (the drivine-cypher-compiler wheel) is loaded on the first Cypher statement compiled for a SQL
store, not on import: an application on the graph engines alone never loads it, and runs where it has no wheel, as on an
Intel Mac or on Alpine (liberation-data/drivine-python#471). There, a statement for a SQL store is refused naming the
platform.
"""

import subprocess
import sys


def run(script: str) -> str:
    """What running the script in a fresh interpreter prints."""
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout.strip()


def test_importing_drivine_and_every_connection_loads_no_compiler():
    script = (
        "import sys\n"
        "import drivine\n"
        "import drivine.connection.neo4j.neo4j_connection, drivine.connection.postgres.postgres_connection\n"
        "import drivine.connection.oracle.oracle_connection, drivine.connection.sqlite.sqlite_connection\n"
        "from drivine.cypher.shared_compiler import shared_compiler_loaded\n"
        "print('drivine_cypher_compiler' in sys.modules, 'antlr4' in sys.modules, shared_compiler_loaded())"
    )
    assert run(script) == "False False False"


def test_a_graph_engines_statement_needs_no_compiler_where_it_has_no_wheel():
    # A Cypher statement for a graph engine is sent as it is: nothing compiles it, so a missing wheel changes nothing.
    script = (
        "import sys\n"
        "sys.modules['drivine_cypher_compiler'] = None\n"
        "from drivine import QuerySpecification, cypher_statement\n"
        "from drivine.connection.neo4j.neo4j_spec_compiler import Neo4jSpecCompiler\n"
        "spec = QuerySpecification(cypher_statement('MATCH (p:Person) RETURN p.name')).bind({'x': 1})\n"
        "compiled = Neo4jSpecCompiler(spec.finalized_copy('CYPHER')).compile()\n"
        "print(compiled.statement)"
    )
    assert run(script) == "MATCH (p:Person) RETURN p.name"


def refusals(has_wheel: bool) -> list[str]:
    """What a compile on each SQL store raises with the compiler missing, has_wheel as given, in a fresh interpreter."""
    script = (
        "import sys\n"
        "sys.modules['drivine_cypher_compiler'] = None\n"
        "import drivine.cypher.shared_compiler as shared\n"
        f"shared.has_wheel = lambda: {has_wheel}\n"
        "from drivine import DrivineError, QuerySpecification, cypher_statement\n"
        "from drivine.cypher.cypher_specification import cypher_compiled\n"
        "for store in ('POSTGRES', 'ORACLE', 'SQLITE'):\n"
        "    try:\n"
        "        cypher_compiled(QuerySpecification(cypher_statement('RETURN 1')), store, 'g')\n"
        "        print('compiled')\n"
        "    except DrivineError as error:\n"
        "        print(str(error).splitlines()[0])\n"
    )
    return run(script).splitlines()


def test_a_sql_stores_statement_without_the_compiler_names_the_extra_that_installs_it_where_it_has_a_wheel():
    assert refusals(True) == [
        "Cypher on Postgres is compiled to SQL by drivine-cypher-compiler, which drivine[postgres] installs: "
        'pip install "drivine[postgres]".',
        "Cypher on Oracle is compiled to SQL by drivine-cypher-compiler, which drivine[oracle] installs: "
        'pip install "drivine[oracle]".',
        "Cypher on SQLite is compiled to SQL by drivine-cypher-compiler, which drivine[sqlite] installs: "
        'pip install "drivine[sqlite]".',
    ]


def test_a_sql_stores_statement_where_the_compiler_has_no_wheel_is_refused_naming_the_platform():
    import platform

    machine = f"{platform.system()} {platform.machine()}"
    lines = refusals(False)
    assert len(lines) == 3 and len(set(lines)) == 1
    assert lines[0].startswith("Cypher is compiled to SQL for Postgres, Oracle and SQLite by drivine-cypher-compiler,")
    assert machine in lines[0] and "python:3.x-slim" in lines[0] and "drivine-cypher-spec" in lines[0]


def test_the_platforms_with_a_wheel_are_the_ones_the_compiler_is_built_for():
    from unittest import mock

    from drivine.cypher import shared_compiler

    def on(sys_platform: str, machine: str, libc: str = "glibc", maxsize: int = 2**63 - 1) -> bool:
        with (
            mock.patch.object(shared_compiler.sys, "platform", sys_platform),
            mock.patch.object(shared_compiler.sys, "maxsize", maxsize),
            mock.patch.object(shared_compiler.platform, "machine", lambda: machine),
            mock.patch.object(shared_compiler.platform, "libc_ver", lambda: (libc, "2.31")),
        ):
            return shared_compiler.has_wheel()

    assert on("darwin", "arm64") and on("linux", "x86_64") and on("linux", "aarch64") and on("win32", "AMD64")
    assert not on("darwin", "x86_64") and not on("linux", "x86_64", libc="") and not on("win32", "ARM64")
    assert not on("win32", "AMD64", maxsize=2**31 - 1) and not on("linux", "x86_64", maxsize=2**31 - 1)


def search_refusals(has_wheel: bool) -> list[str]:
    """What a full-text search on each graph engine raises with the compiler missing, has_wheel as given."""
    script = (
        "import sys\n"
        "sys.modules['drivine_cypher_compiler'] = None\n"
        "import drivine.cypher.shared_compiler as shared\n"
        f"shared.has_wheel = lambda: {has_wheel}\n"
        "from drivine import DrivineError\n"
        "for store in ('NEO4J', 'MEMGRAPH', 'FALKORDB'):\n"
        "    try:\n"
        "        shared.full_text_search(store, None, ['Doc'], ['body'], 'graph', top_k=5)\n"
        "        print('written')\n"
        "    except DrivineError as error:\n"
        "        print(error)\n"
    )
    return run(script).splitlines()


def test_a_graph_engines_full_text_search_is_the_modules_and_names_what_installs_it_where_it_has_a_wheel():
    # The graph engines' extras don't install the module, as it has no wheel for every platform they run on.
    assert search_refusals(True) == [
        f"Full-text search on {engine} is written by drivine-cypher-compiler, the shared module: "
        "pip install drivine-cypher-compiler."
        for engine in ("Neo4j", "Memgraph", "FalkorDB")
    ]


def test_a_graph_engines_full_text_search_where_the_module_has_no_wheel_is_refused_naming_the_platform():
    import platform

    lines = search_refusals(False)
    assert [it.split(" is written")[0] for it in lines] == [
        f"Full-text search on {engine}" for engine in ("Neo4j", "Memgraph", "FalkorDB")
    ]
    assert all(f"{platform.system()} {platform.machine()}" in it and "Every other statement" in it for it in lines)
