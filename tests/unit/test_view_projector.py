"""
The Cypher a view is read by on the graph engines: Drivine for TypeScript's ViewProjector.spec.ts. A path ordered by a
hop's property is read in a CALL on every engine, its rows sorted by ORDER BY before its nodes are collected, as
drivine4j reads it (#553).
"""

from typing import Annotated

from drivine import Direction, GraphPath, GraphView, ModelRegistry, NodeFragment, NodeId, Relationship, Root, hop
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR
from drivine.backend.page import Page


class Person(NodeFragment, labels=["Person"]):
    uuid: Annotated[str, NodeId()]


class City(NodeFragment, labels=["City"]):
    pass


class Director(NodeFragment, labels=["Director"]):
    pass


class Movie(NodeFragment, labels=["Movie"]):
    pass


class Resident(GraphView):
    person: Annotated[Person, Root()]
    city: Annotated[City, Relationship("LIVES_IN")]
    known_by: Annotated[list[Person], Relationship("KNOWS", Direction.INCOMING)] = []


class TestViewStatements:
    def test_projects_each_relationship_as_a_pattern_comprehension_and_requires_required_ones(self):
        assert CypherBackend(NEO4J_GRAMMAR).load_all_views(ModelRegistry().view(Resident)).text == (
            "MATCH (n:Person)\n"
            "WHERE EXISTS { MATCH (n)-[:LIVES_IN]->(:City) }\n"
            "RETURN {root: {props: properties(n), labels: labels(n)}, rels: {"
            "city: head([(n)-[r1:LIVES_IN]->(t1:City) | {props: properties(t1), labels: labels(t1)}]), "
            "knownBy: [(n)<-[r2:KNOWS]-(t2:Person) | {props: properties(t2), labels: labels(t2)}]}} AS result"
        )

    def test_projects_each_relationship_in_a_call_subquery_on_falkordb(self):
        assert CypherBackend(FALKORDB_GRAMMAR).load_all_views(ModelRegistry().view(Resident)).text == (
            "MATCH (n:Person)\n"
            "WHERE size([(n)-[:LIVES_IN]->(:City) | 1]) > 0\n"
            "CALL { WITH n OPTIONAL MATCH (n)-[r1:LIVES_IN]->(t1:City) RETURN head(collect(CASE WHEN t1 IS NOT NULL "
            "THEN {props: properties(t1), labels: labels(t1)} END)) AS v1 }\n"
            "CALL { WITH n OPTIONAL MATCH (n)<-[r2:KNOWS]-(t2:Person) RETURN collect(CASE WHEN t2 IS NOT NULL "
            "THEN {props: properties(t2), labels: labels(t2)} END) AS v2 }\n"
            "RETURN {root: {props: properties(n), labels: labels(n)}, rels: {city: v1, knownBy: v2}} AS result"
        )

    def test_checks_existence_the_way_the_engine_can(self):
        assert CypherBackend(FALKORDB_GRAMMAR).count_views(ModelRegistry().view(Resident), "n.uuid = $id").text == (
            "MATCH (n:Person)\nWHERE (n.uuid = $id) AND size([(n)-[:LIVES_IN]->(:City) | 1]) > 0\n"
            "RETURN count(n) AS count"
        )


class Filmography(GraphView):
    person: Annotated[Person, Root()]
    directors: Annotated[
        list[Director],
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "year"), hop("DIRECTED_BY")], required=True),
    ] = []
    latest: Annotated[Movie | None, GraphPath([hop("DIRECTED", Direction.INCOMING, None, "release year", False)])] = (
        None
    )


ORDERED_ON_NEO4J = (
    "MATCH (n:Person)\n"
    "WHERE EXISTS { MATCH (n)-[:ACTED_IN]->(:Movie)-[:DIRECTED_BY]->(:Director) }\n"
    "CALL { WITH n OPTIONAL MATCH (n)-[:ACTED_IN]->(h1:Movie)-[:DIRECTED_BY]->(t1:Director) "
    "WITH n, t1, h1 ORDER BY h1.year IS NULL, h1.year ASC WITH collect(DISTINCT t1) AS s1 "
    "RETURN [x1 IN s1 | {props: properties(x1), labels: labels(x1)}] AS v1 }\n"
    "CALL { WITH n OPTIONAL MATCH (n)<-[:DIRECTED]-(t2:Movie) "
    "WITH n, t2 ORDER BY t2.`release year` IS NULL, t2.`release year` DESC WITH collect(DISTINCT t2) AS s2 "
    "RETURN head([x2 IN s2 | {props: properties(x2), labels: labels(x2)}]) AS v2 }\n"
    "RETURN {root: {props: properties(n), labels: labels(n)}, rels: {directors: v1, latest: v2}} AS result"
)


class TestOrderedPathStatements:
    def test_sorts_the_rows_in_a_call_before_collecting_the_nodes_on_neo4j_nulls_last(self):
        assert CypherBackend(NEO4J_GRAMMAR).load_all_views(ModelRegistry().view(Filmography)).text == ORDERED_ON_NEO4J

    def test_sorts_the_rows_in_a_call_before_collecting_the_nodes_on_memgraph_as_on_neo4j(self):
        assert (
            CypherBackend(MEMGRAPH_GRAMMAR).load_all_views(ModelRegistry().view(Filmography)).text == ORDERED_ON_NEO4J
        )

    def test_sorts_the_rows_before_collecting_them_on_falkordb_nulls_last(self):
        assert CypherBackend(FALKORDB_GRAMMAR).load_all_views(ModelRegistry().view(Filmography)).text == (
            "MATCH (n:Person)\n"
            "WHERE any(p1 IN [(n)-[:ACTED_IN]->(:Movie)-[:DIRECTED_BY]->(p1) | p1] WHERE p1:Director)\n"
            "CALL { WITH n OPTIONAL MATCH (n)-[:ACTED_IN]->(h1:Movie)-[:DIRECTED_BY]->(t1) "
            "WITH n, t1, h1 ORDER BY h1.year IS NULL, h1.year ASC RETURN collect(DISTINCT CASE WHEN t1:Director "
            "THEN {props: properties(t1), labels: labels(t1)} END) AS v1 }\n"
            "CALL { WITH n OPTIONAL MATCH (n)<-[:DIRECTED]-(t2) "
            "WITH n, t2 ORDER BY t2.`release year` IS NULL, t2.`release year` DESC "
            "RETURN head(collect(DISTINCT CASE WHEN t2:Movie "
            "THEN {props: properties(t2), labels: labels(t2)} END)) AS v2 }\n"
            "RETURN {root: {props: properties(n), labels: labels(n)}, rels: {directors: v1, latest: v2}} AS result"
        )


class Acquaintance(GraphView):
    person: Annotated[Person, Root()]
    friends: Annotated[list[Filmography], Relationship("KNOWS")] = []
    city: Annotated[City | None, Relationship("LIVES_IN")] = None


class TestAViewNestedWithAnOrderedPath:
    def test_reads_the_relationship_to_it_in_a_call_on_neo4j_and_the_others_as_comprehensions(self):
        assert CypherBackend(NEO4J_GRAMMAR).load_all_views(ModelRegistry().view(Acquaintance)).text == (
            "MATCH (n:Person)\n"
            "CALL { WITH n OPTIONAL MATCH (n)-[r1:KNOWS]->(t1:Person) "
            "WHERE EXISTS { MATCH (t1)-[:ACTED_IN]->(:Movie)-[:DIRECTED_BY]->(:Director) } "
            "CALL { WITH t1 OPTIONAL MATCH (t1)-[:ACTED_IN]->(h2:Movie)-[:DIRECTED_BY]->(t2:Director) "
            "WITH t1, t2, h2 ORDER BY h2.year IS NULL, h2.year ASC WITH collect(DISTINCT t2) AS s2 "
            "RETURN [x2 IN s2 | {props: properties(x2), labels: labels(x2)}] AS v2 } "
            "CALL { WITH t1 OPTIONAL MATCH (t1)<-[:DIRECTED]-(t3:Movie) "
            "WITH t1, t3 ORDER BY t3.`release year` IS NULL, t3.`release year` DESC WITH collect(DISTINCT t3) AS s3 "
            "RETURN head([x3 IN s3 | {props: properties(x3), labels: labels(x3)}]) AS v3 } "
            "RETURN collect(CASE WHEN t1 IS NOT NULL THEN {root: {props: properties(t1), labels: labels(t1)}, "
            "rels: {directors: v2, latest: v3}} END) AS v1 }\n"
            "RETURN {root: {props: properties(n), labels: labels(n)}, rels: {friends: v1, "
            "city: head([(n)-[r4:LIVES_IN]->(t4:City) | {props: properties(t4), labels: labels(t4)}])}} AS result"
        )


class Partner(GraphView):
    person: Annotated[Person, Root()]
    spouse: Annotated[Filmography | None, Relationship("MARRIED_TO")] = None


class TestASingleRelationshipToAViewWithAnOrderedPath:
    def test_reads_its_first_target_in_a_call_on_memgraph_and_pages_the_roots_after(self):
        statement = CypherBackend(MEMGRAPH_GRAMMAR).load_all_views(
            ModelRegistry().view(Partner), None, {}, Page("n.uuid", 1, 2)
        )
        assert statement.text == (
            "MATCH (n:Person)\n"
            "CALL { WITH n OPTIONAL MATCH (n)-[r1:MARRIED_TO]->(t1:Person) "
            "WHERE EXISTS { MATCH (t1)-[:ACTED_IN]->(:Movie)-[:DIRECTED_BY]->(:Director) } "
            "CALL { WITH t1 OPTIONAL MATCH (t1)-[:ACTED_IN]->(h2:Movie)-[:DIRECTED_BY]->(t2:Director) "
            "WITH t1, t2, h2 ORDER BY h2.year IS NULL, h2.year ASC WITH collect(DISTINCT t2) AS s2 "
            "RETURN [x2 IN s2 | {props: properties(x2), labels: labels(x2)}] AS v2 } "
            "CALL { WITH t1 OPTIONAL MATCH (t1)<-[:DIRECTED]-(t3:Movie) "
            "WITH t1, t3 ORDER BY t3.`release year` IS NULL, t3.`release year` DESC WITH collect(DISTINCT t3) AS s3 "
            "RETURN head([x3 IN s3 | {props: properties(x3), labels: labels(x3)}]) AS v3 } "
            "RETURN head(collect(CASE WHEN t1 IS NOT NULL THEN {root: {props: properties(t1), labels: labels(t1)}, "
            "rels: {directors: v2, latest: v3}} END)) AS v1 }\n"
            "RETURN {root: {props: properties(n), labels: labels(n)}, rels: {spouse: v1}} AS result\n"
            "ORDER BY n.uuid\nSKIP 1\nLIMIT 2"
        )
