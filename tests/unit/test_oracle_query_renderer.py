"""drivine4j's OraclePredicatesTest for matches, ported: TypeScript's Drivine tests the same."""

from typing import Any

import pytest

from drivine import DrivineError, ModelRegistry, refs
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from tests.models import Person


def matching(pattern: str) -> tuple[str, dict[str, Any]]:
    registry = ModelRegistry()
    return OracleBackend(OracleGraph()).render_where(
        refs(Person).name.matches(pattern), registry.fragment(Person), lambda type_: registry.fragment(type_).labels
    )


def bound(pattern: str) -> str:
    return next(iter(matching(pattern)[1].values()))


def test_matches_holds_an_expression_to_the_whole_value():
    where, bindings = matching("a.*")
    assert where == "REGEXP_LIKE(JSON_VALUE(n.props, '$.\"name\"'), $_q0)"
    assert bindings == {"_q0": "^(a.*)$"}


def test_matches_renumbers_a_backreference_for_the_group_that_anchors_the_expression():
    assert bound(r"(A).*\1") == r"^((A).*\2)$"
    assert bound(r"(a)(b)\2\1") == r"^((a)(b)\3\2)$"
    # An escaped backslash, and a backslash in a bracket expression, are no backreferences.
    assert bound(r"a\\1") == r"^(a\\1)$"
    assert bound(r"[\1]x") == r"^([\1]x)$"
    assert bound(r"[]\1](a)\1") == r"^([]\1](a)\2)$"


def test_matches_refuses_a_reference_to_a_ninth_group_which_would_need_a_tenth():
    with pytest.raises(DrivineError, match="ninth"):
        matching(r"(a)(b)(c)(d)(e)(f)(g)(h)(i)\9")


def test_matches_ignores_case_only_for_a_leading_case_flag_which_is_taken_off_the_expression():
    where, bindings = matching("(?i)a.*")
    assert where == "REGEXP_LIKE(JSON_VALUE(n.props, '$.\"name\"'), $_q0, 'i')"
    assert bindings == {"_q0": "^(a.*)$"}
    assert "'i'" not in matching("a(?i)b")[0]
    assert bound(r"(?i)(a)\1") == r"^((a)\2)$"
