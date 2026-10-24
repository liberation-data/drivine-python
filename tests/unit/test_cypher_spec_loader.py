"""The Cypher spec's files are read as drivine4j reads them: YAML 1.2's core schema, not PyYAML's YAML 1.1."""

import yaml

from tests.cypher.cypher_spec import _Loader  # pyright: ignore[reportPrivateUsage]


def test_a_spec_file_is_read_with_yaml_1_2s_core_schema():
    read = yaml.load(  # noqa: S506
        "[2, 2.0, 1e3, true, yes, on, '10:30', 10:30, 2024-01-01, 2024-01-01T10:00:00Z, ~]", Loader=_Loader
    )
    assert read == [2, 2.0, 1000.0, True, "yes", "on", "10:30", "10:30", "2024-01-01", "2024-01-01T10:00:00Z", None]
    assert [type(it) for it in read[:3]] == [int, float, float]
