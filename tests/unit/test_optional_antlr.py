"""
ANTLR's runtime comes with drivine[postgres] and drivine[oracle], for the Cypher compiler: a connection to either loads
it only to compile a Cypher statement, and without it such a statement is refused with the extra that installs it.
"""

import subprocess
import sys


def run(script: str) -> str:
    """What running the script in a fresh interpreter prints."""
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout.strip()


def test_a_connection_to_a_sql_store_doesnt_load_antlr_until_a_cypher_statement_is_compiled():
    script = (
        "import sys\n"
        "import drivine.connection.postgres.postgres_connection, drivine.connection.oracle.oracle_connection\n"
        "print('antlr4' in sys.modules)"
    )
    assert run(script) == "False"


def test_a_cypher_statement_without_antlr_is_refused_with_the_extra_that_installs_it():
    script = (
        "import sys\n"
        "sys.modules['antlr4'] = None\n"
        "from drivine import DrivineError, QuerySpecification, cypher_statement\n"
        "from drivine.cypher.cypher_specification import cypher_compiled\n"
        "for store in ('POSTGRES', 'ORACLE'):\n"
        "    try:\n"
        "        cypher_compiled(QuerySpecification(cypher_statement('RETURN 1')), store, 'g')\n"
        "    except DrivineError as error:\n"
        "        print(str(error).splitlines()[0])\n"
    )
    assert run(script).splitlines() == [
        f"A Cypher statement is compiled to SQL with ANTLR's runtime, which drivine[{extra}] installs: "
        f'pip install "drivine[{extra}]".'
        for extra in ("postgres", "oracle")
    ]
