from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Literal

QueryLanguage = Literal["CYPHER", "SQL", "PLATFORM_DEFAULT"]


@dataclass(frozen=True)
class Statement:
    text: str
    language: QueryLanguage = "PLATFORM_DEFAULT"

    def to_platform_default(self, language: QueryLanguage) -> Statement:
        """
        This statement in the given language if it is PLATFORM_DEFAULT, otherwise this statement.
        """
        return Statement(self.text, language) if self.language == "PLATFORM_DEFAULT" else self


def cypher_statement(text: str) -> Statement:
    return Statement(text, "CYPHER")


def sql_statement(text: str) -> Statement:
    return Statement(text, "SQL")


def cypher_file(package: str | None, name: str) -> Statement:
    """
    A Cypher statement kept in a file in a package, read with importlib.resources, so it is found in an installed
    wheel or a zip as well as in a source tree: `cypher_file(__package__, "routes_between.cypher")`. The suffix is
    optional.
    """
    return cypher_statement(_read(package, name, ".cypher"))


def sql_file(package: str | None, name: str) -> Statement:
    """
    An SQL statement kept in a file in a package: `sql_file(__package__, "report.sql")`. The suffix is optional.
    """
    return sql_statement(_read(package, name, ".sql"))


def _read(package: str | None, name: str, suffix: str) -> str:
    if not package:
        raise ValueError("A statement file is read from a package: pass __package__ from a module in one.")
    if not name.endswith(suffix):
        name += suffix
    return resources.files(package).joinpath(name).read_text(encoding="utf-8")
