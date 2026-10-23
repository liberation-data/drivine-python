"""What drivine4j names the objects a SQL store makes beside its tables: this library names them alike, to the hash."""

import json
from pathlib import Path

from drivine.backend.oracle.oracle_graph import OracleGraph

DRIVINE4J = json.loads((Path(__file__).parent.parent / "golden" / "drivine4j-oracle-schema.json").read_text())
"""What drivine4j wrote of a store's schema on Oracle, and the names it gives objects (see its `source`)."""


def test_names_objects_as_drivine4j_names_them_to_the_hash_of_javas_string_hash_code():
    names = DRIVINE4J["objectNames"]
    assert len(names) > 10
    for it in names:
        assert OracleGraph(it["graph"]).object_name(it["kind"], it["parts"]) == it["name"]
