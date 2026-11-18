"""The spec runner's holds (tests/cypher/cypher_spec.py `HELD`): a held step must fail, and a skipped one isn't run."""

from tests.cypher.cypher_spec import held_for, load, skipped_for

QUERIES = load("queries.yaml")


def test_collect_skips_nulls_is_skipped_on_oracle_alone_and_held_on_no_store():
    # Oracle's server sometimes ends the session in this case, which passes in most runs (drivine4j#838).
    case = next(it for it in QUERIES.cases if it.name == "collect skips nulls")
    assert skipped_for("queries.yaml", case, 0, "oracle") == "drivine4j#838"
    for store in ("postgres", "sqlite", "neo4j"):
        assert skipped_for("queries.yaml", case, 0, store) is None
    for store in ("oracle", "postgres", "sqlite", "neo4j"):
        assert held_for("queries.yaml", case, 0, store) is None
