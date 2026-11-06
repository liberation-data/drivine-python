"""
`schema_ddl.translate`: Neo4j's schema DDL read as Drivine's specs, with no store. tests/cypher/schema.yaml pins the
shapes the three libraries share. drivine4j's SchemaDdlTest, ported.
"""

from __future__ import annotations

import pytest

from drivine import DrivineError
from drivine.connection.database_type import DatabaseType
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sqlite_cypher_compiler import SqliteCypherCompiler
from drivine.query.query_specification import QuerySpecification
from drivine.query.statement import cypher_statement
from drivine.schema.engine_vector_options import EngineVectorOptions
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.schema_ddl import refused_in_transaction, translate
from drivine.schema.schema_ddl_result import (
    AWAIT_INDEXES,
    NOT_DDL,
    Create,
    Drop,
    Refused,
    Reserved,
    TextIndexSpec,
)
from drivine.schema.schema_specs import item_name, range_index, uniqueness_constraint, vector_index


def refused(text: str) -> str:
    result = translate(text)
    assert isinstance(result, Refused), text
    return result.message


def created(text: str) -> Create:
    result = translate(text)
    assert isinstance(result, Create), text
    return result


def test_the_schema_file_of_an_apoc_export_reads_as_the_specs_the_schema_api_declares() -> None:
    assert translate(
        "CREATE FULLTEXT INDEX Chunk_text_fulltext IF NOT EXISTS FOR (n:Chunk) ON EACH [n.`text`];"
    ) == Create(FullTextIndexSpec("Chunk", ("text",)), None, True)
    assert translate("CREATE RANGE INDEX IF NOT EXISTS FOR (n:ChatSession) ON (n.lastActivityAt);") == Create(
        range_index("ChatSession", "lastActivityAt"), None, True
    )
    assert translate(
        "CREATE RANGE INDEX IF NOT EXISTS FOR (n:ContentElement) ON (n.root_document_id, n.ordinal);"
    ) == Create(range_index("ContentElement", ["root_document_id", "ordinal"]), None, True)
    assert translate(
        "CREATE CONSTRAINT Attachment_attachmentId_unique IF NOT EXISTS FOR (node:Attachment) "
        "REQUIRE (node.attachmentId) IS UNIQUE;"
    ) == Create(uniqueness_constraint("Attachment", "attachmentId"), None, True)
    assert translate(
        "CREATE CONSTRAINT _DrivineSchema_scope_unique IF NOT EXISTS FOR (node:_DrivineSchema) "
        "REQUIRE (node.scope) IS UNIQUE;"
    ) == Reserved(uniqueness_constraint("_DrivineSchema", "scope"))
    assert translate(
        "CREATE CONSTRAINT UNIQUE_IMPORT_NAME IF NOT EXISTS FOR (node:`UNIQUE IMPORT LABEL`) "
        "REQUIRE (node.`UNIQUE IMPORT ID`) IS UNIQUE;"
    ) == Create(uniqueness_constraint("UNIQUE IMPORT LABEL", "UNIQUE IMPORT ID"), "UNIQUE_IMPORT_NAME", True)
    assert translate("CALL db.awaitIndexes(300);") is AWAIT_INDEXES
    assert translate("DROP CONSTRAINT UNIQUE_IMPORT_NAME;") == Drop("CONSTRAINT", "UNIQUE_IMPORT_NAME", False)


def test_a_spec_is_unnamed_and_the_ddls_name_is_its_alias_only_where_the_schema_api_names_it_otherwise() -> None:
    named = created("CREATE INDEX person_name FOR (p:Person) ON (p.name)")
    assert named.alias == "person_name"
    assert item_name(named.spec) == "Person_name_range"  # pyright: ignore[reportArgumentType]
    assert created("CREATE INDEX Person_name_range FOR (p:Person) ON (p.name)").alias is None
    assert created("CREATE CONSTRAINT FOR (p:Person) REQUIRE p.email IS UNIQUE").alias is None


def test_each_form_of_a_range_index_and_a_uniqueness_constraint_neo4j_4s_among_them() -> None:
    both = Create(range_index("Person", ["name", "born"]), None, False)
    for text in (
        "CREATE INDEX FOR (p:Person) ON (p.name, p.born)",
        "CREATE RANGE INDEX FOR (p:Person) ON (p.name, p.born)",
        "CREATE BTREE INDEX FOR (p:Person) ON (p.name, p.born)",
        "CREATE INDEX ON :Person(name, born)",
        "create index for (p:Person) on (p.name, p.born) options {indexProvider: 'range-1.0'}",
    ):
        assert translate(text) == both, text
    assert translate("CREATE INDEX by_name IF NOT EXISTS FOR (p:Person) ON p.name") == Create(
        range_index("Person", "name"), "by_name", True
    )

    unique = Create(uniqueness_constraint("Person", "email"), None, False)
    for text in (
        "CREATE CONSTRAINT FOR (p:Person) REQUIRE p.email IS UNIQUE",
        "CREATE CONSTRAINT FOR (p:Person) REQUIRE (p.email) IS NODE UNIQUE",
        "CREATE CONSTRAINT ON (p:Person) ASSERT p.email IS UNIQUE",
    ):
        assert translate(text) == unique, text
    assert translate("CREATE CONSTRAINT email IF NOT EXISTS ON (p:Person) ASSERT (p.email) IS UNIQUE") == Create(
        uniqueness_constraint("Person", "email"), "email", True
    )
    assert translate("CREATE CONSTRAINT FOR (p:Person) REQUIRE (p.first, p.last) IS UNIQUE") == Create(
        uniqueness_constraint("Person", ["first", "last"]), None, False
    )


def test_a_full_text_index_takes_its_analyzer_a_vector_index_its_options_and_a_text_index_is_neo4js() -> None:
    assert translate(
        "CREATE FULLTEXT INDEX titles FOR (n:Book) ON EACH [n.title, n.subtitle] OPTIONS {indexConfig: "
        "{`fulltext.analyzer`: 'english', `fulltext.eventually_consistent`: true}}"
    ) == Create(FullTextIndexSpec("Book", ("title", "subtitle"), analyzer="english"), "titles", False)
    assert translate(
        "CREATE VECTOR INDEX FOR (c:Chunk) ON (c.embedding) OPTIONS {indexProvider: 'vector-2.0', indexConfig: "
        "{`vector.dimensions`: 3, `vector.similarity_function`: 'EUCLIDEAN', `vector.hnsw.m`: 16, "
        "`vector.hnsw.ef_construction`: 100, `vector.quantization.enabled`: false}}"
    ) == Create(
        vector_index(
            "Chunk",
            "embedding",
            3,
            "EUCLIDEAN",
            hnsw_m=16,
            hnsw_ef_construction=100,
            engine_options=[EngineVectorOptions("Neo4j", quantization_enabled=False)],
        ),
        None,
        False,
    )
    assert translate(
        "CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS FOR (c:Chunk) ON c.embedding OPTIONS {indexConfig: "
        "{`vector.dimensions`: 1536, `vector.similarity_function`: 'cosine'}}"
    ) == Create(vector_index("Chunk", "embedding", 1536), "chunk_embedding", True)
    assert translate("CREATE TEXT INDEX FOR (p:Person) ON (p.name)") == Create(
        TextIndexSpec("Person", ("name",)), None, False
    )


def test_a_name_in_backticks_holds_spaces_and_a_doubled_backtick_in_it_stands_for_one() -> None:
    made = created(
        "CREATE CONSTRAINT `the ``key`` of a thing` FOR (`a node`:`A Thing`) REQUIRE (`a node`.`the key`) IS UNIQUE"
    )
    assert made.spec == uniqueness_constraint("A Thing", "the key")
    assert made.alias == "the `key` of a thing"
    assert translate("drop index `by name` if exists") == Drop("INDEX", "by name", True)
    assert translate("DROP CONSTRAINT `a``b`") == Drop("CONSTRAINT", "a`b", False)
    assert refused("CREATE INDEX `unclosed FOR (p:Person) ON (p.name)").startswith("Invalid schema command:")


def test_db_await_indexes_and_await_index_with_arguments_or_none_do_nothing() -> None:
    for text in (
        "CALL db.awaitIndexes",
        "CALL db.awaitIndexes()",
        "CALL db.awaitIndexes(300)",
        "CALL db.awaitIndex('x', 300);",
        "CALL db.awaitIndexes($t)",
    ):
        assert translate(text) is AWAIT_INDEXES, text


def test_cypher_that_uses_the_words_is_cypher() -> None:
    for text in (
        "CREATE index = (:A)-[:R]->(:B)",
        "CREATE constraint = (:A)",
        "CREATE RANGE = (:A)",
        "CREATE (index:Index {name: 'CREATE INDEX'})",
        "MATCH (index:Index) RETURN index",
        "CALL db.labels()",
        "CALL db.index.fulltext.queryNodes('x', 'y')",
        "RETURN 'DROP INDEX x'",
        "DROP DATABASE x",
        "  // a comment\n MATCH (n) RETURN n",
        "",
    ):
        assert translate(text) is NOT_DDL, text


def test_what_a_sql_store_cannot_honour_is_refused_by_name() -> None:
    assert refused("CREATE POINT INDEX FOR (p:Place) ON (p.location)") == (
        "A POINT index is Neo4j's, and is not made on a SQL store."
    )
    assert refused("CREATE LOOKUP INDEX FOR (n) ON EACH labels(n)").startswith("A LOOKUP index")
    assert refused("CREATE INDEX FOR ()-[r:KNOWS]-() ON (r.since)") == (
        "An index on relationships is not made on a SQL store: Drivine's schema is of nodes."
    )
    assert refused("CREATE CONSTRAINT FOR ()-[r:R]-() REQUIRE r.id IS UNIQUE") == (
        "A constraint on relationships is not made on a SQL store: Drivine's schema is of nodes."
    )
    assert refused("CREATE VECTOR INDEX FOR ()-[r:R]-() ON r.e") == (
        "A vector index on relationships is not made on a SQL store: Drivine's schema is of nodes."
    )
    assert refused("CREATE INDEX FOR (n:A|B) ON (n.p)") == (
        "An index over several labels is not made on a SQL store: declare one for each label."
    )
    assert refused("CREATE CONSTRAINT FOR (n:A:B) REQUIRE n.p IS UNIQUE") == (
        "A constraint over several labels is not made on a SQL store: declare one for each label."
    )
    existence = (
        "A property existence constraint (IS NOT NULL) is not made on a SQL store: Drivine's schema has uniqueness "
        "constraints only."
    )
    assert refused("CREATE CONSTRAINT FOR (p:P) REQUIRE p.name IS NOT NULL") == existence
    assert refused("CREATE CONSTRAINT ON (p:P) ASSERT exists(p.name)") == existence
    key = "A key constraint (IS NODE KEY) is not made on a SQL store: Drivine's schema has uniqueness constraints only."
    assert refused("CREATE CONSTRAINT FOR (p:P) REQUIRE (p.a, p.b) IS NODE KEY") == key
    assert refused("CREATE CONSTRAINT FOR (p:P) REQUIRE p.a IS KEY") == key
    assert refused("CREATE CONSTRAINT FOR (p:P) REQUIRE p.a IS :: STRING").startswith("A property type constraint")
    assert refused("CREATE CONSTRAINT FOR (p:P) REQUIRE p.a IS TYPED STRING").startswith("A property type constraint")
    assert refused("CREATE VECTOR INDEX FOR (c:C) ON (c.a, c.b) OPTIONS {indexConfig: {`vector.dimensions`: 3}}") == (
        "A vector index is over one property, and this names 2."
    )
    assert refused("CREATE VECTOR INDEX FOR (c:C) ON c.e WITH [c.l] OPTIONS {}") == (
        "A vector index WITH properties to filter by is not made on a SQL store."
    )
    assert "`vector.dimensions`" in refused("CREATE VECTOR INDEX FOR (c:C) ON c.e")
    for dimensions in ("0", "true", "1.5"):
        message = refused(
            f"CREATE VECTOR INDEX FOR (c:C) ON c.e OPTIONS {{indexConfig: {{`vector.dimensions`: {dimensions}}}}}"
        )
        assert "positive whole number" in message, dimensions
    assert (
        refused(
            "CREATE VECTOR INDEX FOR (c:C) ON c.e OPTIONS {indexConfig: {`vector.dimensions`: 3, "
            "`vector.similarity_function`: 'dot'}}"
        )
        == "A vector index's `vector.similarity_function` is 'cosine' or 'euclidean', not 'dot'."
    )
    assert refused("DROP CONSTRAINT ON (p:P) ASSERT p.a IS UNIQUE").startswith("DROP CONSTRAINT ON … is Neo4j 4's")
    assert "parameter" in refused("DROP INDEX $name")
    assert refused("CREATE INDEX FOR (m:_DrivineSchema) ON (m.version)").startswith(
        "_DrivineSchema is the label of Drivine's own nodes"
    )
    assert translate("CREATE CONSTRAINT stamp FOR (s:DrivineStore) REQUIRE s.key IS UNIQUE") == Reserved(
        uniqueness_constraint("DrivineStore", "key")
    )


def test_a_schema_command_not_well_formed_is_refused_saying_what_was_expected() -> None:
    assert refused("CREATE INDEX FOR (p:Person)") == "Invalid schema command: expected ON, but found the end."
    assert refused("CREATE INDEX x TO (p:Person)") == "Invalid schema command: expected FOR, but found 'TO'."
    assert refused("CREATE INDEX FOR (p:Person) ON (q.name)") == "The property q.… is of q, and the pattern binds p."
    assert "runs on its own" in refused("CREATE INDEX FOR (p:Person) ON (p.name); MATCH (n) RETURN n")
    assert "empty" in refused("CREATE INDEX FOR (p:Person) ON (p.``)")


def test_an_unterminated_name_or_string_is_refused_unicode_names_read_as_written_and_parameters_are_refused() -> None:
    backtick = "Invalid schema command: A name in backticks has no closing backtick."
    assert refused("CREATE INDEX `by name FOR (p:Person) ON (p.name)") == backtick
    assert refused("CREATE INDEX FOR (p:`Person) ON (p.name)") == backtick
    assert refused("DROP INDEX `by name") == backtick
    assert refused("CREATE INDEX FOR (p:Person) ON (p.name) OPTIONS {indexProvider: 'range}") == (
        "Invalid schema command: A string has no closing quote."
    )
    # Not DDL by its first words, so the Cypher reader is the one to refuse it.
    assert translate("CREATE (n:`Person {name: 'x'})") is NOT_DDL
    assert translate("CREATE `index` = (a)-[:R]->(b)") is NOT_DDL

    assert translate("CREATE INDEX 名前 FOR (p:Persön) ON (p.naïve)") == Create(
        range_index("Persön", "naïve"), "名前", False
    )
    assert translate("CREATE INDEX FOR (p:`Ünï cødé 🙂`) ON (p.`🙂`) /* unterminated") == Create(
        range_index("Ünï cødé 🙂", "🙂"), None, False
    )

    assert "parameter" in refused("CREATE INDEX $name FOR (p:Person) ON (p.name)")
    assert refused("CREATE INDEX FOR (p:Person) ON (p.name) OPTIONS $options").startswith(
        "OPTIONS given as a parameter"
    )
    assert "runs on its own" in refused("CALL db.awaitIndexes() YIELD x")


@pytest.mark.parametrize("compiler", [PostgresCypherCompiler, OracleCypherCompiler, SqliteCypherCompiler])
def test_the_sql_compilers_refuse_a_schema_command_as_the_translator_does_or_as_no_query(compiler: type) -> None:
    with pytest.raises(DrivineError) as point:
        compiler("g").compile("CREATE POINT INDEX FOR (p:Place) ON (p.location)")
    assert str(point.value) == "A POINT index is Neo4j's, and is not made on a SQL store."
    with pytest.raises(DrivineError) as command:
        compiler("g").compile("CREATE INDEX FOR (p:Person) ON (p.name)")
    assert str(command.value).startswith("The statement is a schema command")
    compiler("g").compile("CREATE (index:Index) RETURN index")


def test_a_schema_command_inside_a_transaction_is_refused_naming_the_store_as_it_is_written() -> None:
    spec = QuerySpecification[object](cypher_statement("CREATE INDEX FOR (p:Person) ON (p.name)"))
    for type_, engine in (
        (DatabaseType.POSTGRES, "Postgres"),
        (DatabaseType.ORACLE, "Oracle"),
        (DatabaseType.SQLITE, "SQLite"),
    ):
        with pytest.raises(DrivineError) as refusal:
            refused_in_transaction(type_, spec)
        assert f"not run inside a transaction on {engine}:" in str(refusal.value)
    assert refused_in_transaction(DatabaseType.NEO4J, spec) is False
    # Cypher that is no DDL, and DDL sent as SQL, run as they are.
    assert refused_in_transaction(DatabaseType.POSTGRES, QuerySpecification[object]("CREATE INDEX x ON t (a)")) is False
    awaits = QuerySpecification[object](cypher_statement("CALL db.awaitIndexes(300)"))
    assert refused_in_transaction(DatabaseType.ORACLE, awaits) is True
