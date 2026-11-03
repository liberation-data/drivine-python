"""
Managers resolving the store identity at once, each making the key's constraint: #298, as drivine4j#473. The fake store
runs its schema statements through the real ConstraintManager and SchemaExecutor, async and sync, so a deadlock making
the constraint is run again as it would be.
"""

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from neo4j.exceptions import Neo4jError

from drivine.backend.backends import backend_for
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.manager.contention import CONTENDED_ATTEMPTS
from drivine.query.query_specification import QuerySpecification
from drivine.schema.constraint_manager import ConstraintManager
from drivine.schema.schema_executor import SchemaExecutor
from drivine.schema.schema_grammar import NEO4J_SCHEMA
from drivine.store.store_identity_resolver import StoreIdentityResolver
from drivine.sync.schema.constraint_manager import ConstraintManager as SyncConstraintManager
from drivine.sync.schema.schema_executor import SchemaExecutor as SyncSchemaExecutor
from drivine.sync.store.store_identity_resolver import StoreIdentityResolver as SyncStoreIdentityResolver

Api = Literal["async", "sync"]
APIS: list[Api] = ["async", "sync"]
EVERY_TIME = 1_000_000

DEADLOCK = "Neo.TransientError.Transaction.DeadlockDetected"
CONFLICT = "Cannot resolve conflicting transactions. You can retry this transaction."


def bolt(code: str, message: str | None = None) -> Neo4jError:
    """An error as the driver makes one from the server's failure."""
    return Neo4jError._hydrate_neo4j(code=code, message=message or code)  # pyright: ignore[reportPrivateUsage]


def violated() -> Neo4jError:
    return bolt(
        "Neo.ClientError.Schema.ConstraintValidationFailed",
        "Node(0) already exists with label `DrivineStore` and property `key` = 'singleton'",
    )


class PostgresUniqueViolation(Exception):
    """
    A unique violation as psycopg raises one: its SQLSTATE, the constraint it names in its diagnostics, and a message
    that carries the DETAIL, which says "already exists" of any violation.
    """

    def __init__(self, constraint: str) -> None:
        super().__init__(
            f'duplicate key value violates unique constraint "{constraint}"\nDETAIL:  Key (id)=(7) already exists.'
        )
        self.sqlstate = "23505"
        self.diag = SimpleNamespace(constraint_name=constraint)


class Store:
    """
    A Neo4j store whose stamp MERGE fails with the next of `merge_failures`, if any. The first `create_failures`
    CREATE CONSTRAINTs deadlock, or, given `made_meanwhile`, are refused with it, another session having made the
    constraint, unless `found_after` is False: then nothing made it, and the refusal is for something else. Given
    `listed_by`, another manager's constraint is listed from the first SHOW CONSTRAINTS for which it answers True, told
    how many CREATEs and SHOWs there have been.
    """

    def __init__(
        self,
        create_failures: int,
        merge_failures: list[BaseException],
        made_meanwhile: BaseException | None = None,
        found_after: bool = True,
        listed_by: Callable[[int, int], bool] | None = None,
    ) -> None:
        self.create_failures = create_failures
        self.merge_failures = merge_failures
        self.made_meanwhile = made_meanwhile
        self.found_after = found_after
        self.listed_by = listed_by
        self.created = False
        self.creates = 0
        self.shows = 0
        self.merges = 0
        self.pauses: list[float] = []
        """The pauses the resolver took between looks for the winner's constraint, given a sleep that doesn't wait."""

    def query(self, spec: QuerySpecification[Any]) -> list[Any]:
        assert spec.statement is not None
        statement = spec.statement.text
        if statement.startswith("CREATE CONSTRAINT"):
            self.creates += 1
            if self.creates <= self.create_failures:
                self.created = self.made_meanwhile is not None and self.found_after
                raise self.made_meanwhile or bolt(DEADLOCK, "Deadlock wait cycle.")
            self.created = True
        if statement.startswith("SHOW CONSTRAINTS"):
            self.shows += 1
            self.created = self.created or (self.listed_by is not None and self.listed_by(self.creates, self.shows))
        if statement.startswith("SHOW CONSTRAINTS") and self.created:
            return [{"name": "DrivineStore_key", "labels": ["DrivineStore"], "properties": ["key"]}]
        return []

    def execute(self) -> None:
        self.merges += 1
        if self.merge_failures:
            failure = self.merge_failures.pop(0)
            raise DrivineError.with_root_cause(failure) from failure

    def stamps(self) -> list[dict[str, str]]:
        return [{"id": "store-8", "assignedAt": "2026-01-01T00:00:00Z"}]


class AsyncConnection:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def query(self, spec: QuerySpecification[Any]) -> list[Any]:
        return self.store.query(spec)

    async def release(self, error: BaseException | None = None) -> None:
        pass


class SyncConnection:
    def __init__(self, store: Store) -> None:
        self.store = store

    def query(self, spec: QuerySpecification[Any]) -> list[Any]:
        return self.store.query(spec)

    def release(self, error: BaseException | None = None) -> None:
        pass


class AsyncProvider:
    name = "neo4j"
    type = DatabaseType.NEO4J

    def __init__(self, store: Store) -> None:
        self.store = store

    async def connect(self) -> AsyncConnection:
        return AsyncConnection(self.store)


class SyncProvider:
    name = "neo4j"
    type = DatabaseType.NEO4J

    def __init__(self, store: Store) -> None:
        self.store = store

    def connect(self) -> SyncConnection:
        return SyncConnection(self.store)


class AsyncPm:
    database = "neo4j"
    type = DatabaseType.NEO4J

    def __init__(self, store: Store) -> None:
        self.store = store

    async def execute(self, spec: QuerySpecification[Any]) -> None:
        self.store.execute()

    async def query(self, spec: QuerySpecification[Any]) -> list[Any]:
        return self.store.stamps()


class SyncPm:
    database = "neo4j"
    type = DatabaseType.NEO4J

    def __init__(self, store: Store) -> None:
        self.store = store

    def execute(self, spec: QuerySpecification[Any]) -> None:
        self.store.execute()

    def query(self, spec: QuerySpecification[Any]) -> list[Any]:
        return self.store.stamps()


async def resolve(store: Store, api: Api, schema_management: bool = True) -> str:
    """The id the API's resolver gives over the store, with schema management or (as on Neptune) without it."""
    backend = backend_for(DatabaseType.NEO4J)
    if api == "async":
        constraints = ConstraintManager(SchemaExecutor(AsyncProvider(store)), NEO4J_SCHEMA)  # pyright: ignore[reportArgumentType]

        async def pause(seconds: float) -> None:
            store.pauses.append(seconds)

        resolver = StoreIdentityResolver(AsyncPm(store), backend, constraints if schema_management else None, pause)  # pyright: ignore[reportArgumentType]
        return (await resolver.resolve()).id
    sync_constraints = SyncConstraintManager(SyncSchemaExecutor(SyncProvider(store)), NEO4J_SCHEMA)  # pyright: ignore[reportArgumentType]
    sync_resolver = SyncStoreIdentityResolver(
        SyncPm(store), backend, sync_constraints if schema_management else None, store.pauses.append
    )  # pyright: ignore[reportArgumentType]
    return sync_resolver.resolve().id


def best_effort(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [it.getMessage() for it in caplog.records if "best-effort" in it.getMessage()]


@pytest.fixture(autouse=True)
def warnings(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, "drivine.store")


@pytest.mark.parametrize("api", APIS)
async def test_a_stamp_the_constraint_refuses_is_written_again_though_its_making_deadlocked(api: Api):
    # The refusal itself proves a constraint in force, so it is taken for a lost race though none was confirmed.
    store = Store(EVERY_TIME, [violated()])

    assert await resolve(store, api) == "store-8"
    assert store.creates > 1, "the schema executor ran the deadlocked statement again"
    assert store.merges == 2


@pytest.mark.parametrize("api", APIS)
async def test_a_constraint_whose_making_deadlocked_once_is_made_again_so_the_stamp_is_written_under_it(api: Api):
    # Only under a constraint confirmed is a conflict taken for a lost race, and the stamp written again.
    store = Store(1, [Exception(CONFLICT)])

    assert await resolve(store, api) == "store-8"
    assert store.creates == 2 and store.merges == 2


@pytest.mark.parametrize("api", APIS)
async def test_the_constraint_is_ensured_again_when_another_manager_made_it_meanwhile(api: Api, caplog):
    # As FalkorDB refuses a second constraint on the key.
    store = Store(1, [Exception(CONFLICT)], made_meanwhile=Exception("Constraint already exists."))

    assert await resolve(store, api) == "store-8"
    # The second ensure finds the other's constraint, so it isn't made again, and the stamp is written under it.
    assert store.creates == 1 and store.merges == 2
    assert best_effort(caplog) == []


@pytest.mark.parametrize("api", APIS)
async def test_the_constraint_is_ensured_again_when_another_manager_made_the_index_it_needs_meanwhile(api: Api, caplog):
    store = Store(1, [], made_meanwhile=Exception("Attribute 'key' is already indexed"))

    assert await resolve(store, api) == "store-8"
    assert store.creates == 1 and best_effort(caplog) == []


@pytest.mark.parametrize("api", APIS)
async def test_the_constraint_is_ensured_again_when_postgres_refused_its_index_for_a_name_taken_in_pg_class(
    api: Api, caplog
):
    raced = PostgresUniqueViolation("pg_class_relname_nsp_index")
    store = Store(1, [], made_meanwhile=DrivineError.with_root_cause(raced))

    assert await resolve(store, api) == "store-8"
    assert store.creates == 1 and best_effort(caplog) == []


@pytest.mark.parametrize("api", APIS)
async def test_a_unique_violation_in_a_users_table_is_not_taken_for_one_made_meanwhile(api: Api, caplog):
    violation = PostgresUniqueViolation("orders_pkey")
    store = Store(1, [], made_meanwhile=DrivineError.with_root_cause(violation), found_after=False)

    assert await resolve(store, api) == "store-8"
    assert store.creates == 1
    assert len(best_effort(caplog)) == 1 and "orders_pkey" in best_effort(caplog)[0]


@pytest.mark.parametrize("api", APIS)
async def test_identity_is_best_effort_once_its_making_is_refused_as_made_meanwhile_three_times(api: Api, caplog):
    # A real misconfiguration: something in the way that ensure can't find, so the constraint is never there.
    refusal = Exception("There already exists an index (:DrivineStore {key}).")
    store = Store(EVERY_TIME, [], made_meanwhile=refusal, found_after=False)

    assert await resolve(store, api) == "store-8"
    assert store.creates == 3 and store.merges == 1
    assert len(best_effort(caplog)) == 1 and "There already exists an index" in best_effort(caplog)[0]


@pytest.mark.parametrize("api", APIS)
async def test_a_constraint_refusal_of_another_kind_is_not_ensured_again(api: Api, caplog):
    store = Store(
        EVERY_TIME,
        [],
        made_meanwhile=bolt("Neo.ClientError.Security.Forbidden", "Schema operation denied"),
        found_after=False,
    )

    assert await resolve(store, api) == "store-8"
    assert store.creates == 1 and len(best_effort(caplog)) == 1


@pytest.mark.parametrize("api", APIS)
async def test_the_winners_constraint_is_found_when_its_own_making_deadlocked_as_often_as_the_executor_runs_one(
    api: Api, caplog
):
    # drivine CI run 41260212023's 16 managers: this one lost every round of the label's lock, while another made it.
    store = Store(EVERY_TIME, [Exception(CONFLICT)], listed_by=lambda made, _: made >= CONTENDED_ATTEMPTS)

    assert await resolve(store, api) == "store-8"
    assert store.creates == CONTENDED_ATTEMPTS
    # Under the constraint found, a conflict is a lost race, and the stamp is written again.
    assert store.merges == 2
    assert best_effort(caplog) == []


@pytest.mark.parametrize("api", APIS)
async def test_the_winners_constraint_is_looked_for_again_after_a_pause_while_the_winner_is_still_making_it(
    api: Api, caplog
):
    # The ensure's first look, then the first two looks after the deadlocks, find nothing.
    store = Store(EVERY_TIME, [], listed_by=lambda made, shows: made >= CONTENDED_ATTEMPTS and shows > 3)

    assert await resolve(store, api) == "store-8"
    assert store.creates == CONTENDED_ATTEMPTS and best_effort(caplog) == []


@pytest.mark.parametrize("api", APIS)
async def test_the_winners_constraint_is_looked_for_only_so_long_after_the_deadlocks_and_then_warned_of(
    api: Api, caplog
):
    store = Store(EVERY_TIME, [], listed_by=lambda _, __: False)

    assert await resolve(store, api) == "store-8"
    # The ensure's own look, the resolver's eight (_LOOKS_FOR_THE_WINNER) after it gave up, then the last ensure's.
    assert store.shows == 1 + 8 + 1
    # Before each look but the first, as many 50 ms as looks so far, and by chance up to as much again.
    assert len(store.pauses) == 7
    assert all(n * 0.05 <= pause < n * 0.1 for n, pause in enumerate(store.pauses, start=1)), store.pauses
    assert len(best_effort(caplog)) == 1


@pytest.mark.parametrize("api", APIS)
async def test_the_constraint_is_ensured_once_more_when_the_looks_for_the_winners_find_none(api: Api, caplog):
    # The other managers have stopped making it, or given up: the last ensure makes it, so no stamp goes unguarded.
    store = Store(CONTENDED_ATTEMPTS, [Exception(CONFLICT)])

    assert await resolve(store, api) == "store-8"
    assert store.creates == CONTENDED_ATTEMPTS + 1 and len(store.pauses) == 7
    # Under the constraint made, a conflict is a lost race, and the stamp is written again.
    assert store.merges == 2
    assert best_effort(caplog) == []


@pytest.mark.parametrize("api", APIS)
async def test_the_winners_constraint_is_looked_for_again_after_an_oracle_ddl_deadlock(api: Api, caplog):
    # ORA-04020, on an object's library cache lock, is contention as drivine4j's resolver takes it, so it is looked for
    # again rather than once. The ensure's own look and the first after the deadlocks find nothing.
    deadlock = OracleFailure("ORA-04020: deadlock detected while trying to lock object DRIVINE.DRIVINE_NODE")
    store = Store(
        EVERY_TIME,
        [],
        made_meanwhile=deadlock,
        found_after=False,
        listed_by=lambda made, shows: made >= CONTENDED_ATTEMPTS and shows > 2,
    )

    assert await resolve(store, api) == "store-8"
    assert store.creates == CONTENDED_ATTEMPTS and best_effort(caplog) == []


@pytest.mark.parametrize("api", APIS)
async def test_the_winners_constraint_is_looked_for_once_without_a_pause_after_a_refusal_that_is_not_contention(
    api: Api, caplog
):
    # No permission to make it: no winner is still making it, so the warning doesn't wait out the looks.
    forbidden = bolt("Neo.ClientError.Security.Forbidden", "Schema operation denied")
    store = Store(EVERY_TIME, [], made_meanwhile=forbidden, found_after=False, listed_by=lambda _, __: False)

    assert await resolve(store, api) == "store-8"
    # The ensure's own look, then the resolver's one.
    assert store.creates == 1 and store.shows == 1 + 1
    assert store.pauses == []
    assert len(best_effort(caplog)) == 1


@pytest.mark.parametrize("api", APIS)
async def test_a_stamp_refused_for_another_reason_after_the_deadlock_is_raised_at_once(api: Api):
    refused = bolt("Neo.ClientError.Statement.SyntaxError", "Invalid input")
    store = Store(EVERY_TIME, [refused])

    with pytest.raises(DrivineError) as raised:
        await resolve(store, api)

    assert raised.value.root_cause is refused and store.merges == 1


@pytest.mark.parametrize("api", APIS)
async def test_a_stamp_refused_by_a_constraint_that_is_not_a_uniqueness_one_is_raised_at_once(api: Api):
    # Only a uniqueness violation proves the key's constraint in force; a not-null one fails the same way each time.
    refused = Exception('null value in column "props" of relation "drivine_node" violates not-null constraint')
    store = Store(EVERY_TIME, [refused])

    with pytest.raises(DrivineError) as raised:
        await resolve(store, api)

    assert raised.value.root_cause is refused and store.merges == 1


class PostgresFailure(Exception):
    """A failure as psycopg raises one: its SQLSTATE beside its message."""

    def __init__(self, sqlstate: str, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class OracleFailure(Exception):
    """A failure as python-oracledb raises one: its error, which carries the code that leads the message."""

    def __init__(self, message: str) -> None:
        super().__init__(SimpleNamespace(full_code=message.split(":")[0], message=message))
        self.message = message

    def __str__(self) -> str:
        return self.message


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "refused",
    [
        pytest.param(PostgresFailure("42P07", 'relation "drivine_node" already exists'), id="postgres-42P07"),
        pytest.param(
            OracleFailure("ORA-02261: such unique or primary key already exists in the table"), id="oracle-ORA-02261"
        ),
        pytest.param(
            OracleFailure("ORA-02261: such unique or primary key already exists\nORA-06512: after ORA-00001"),
            id="oracle-ORA-02261-quoting-ORA-00001",
        ),
    ],
)
async def test_a_stamp_refused_for_something_else_that_already_exists_is_raised_at_once(
    api: Api, refused: BaseException
):
    # Where the store gives a code, only its uniqueness violation's (23505, ORA-00001) proves the key's constraint.
    store = Store(EVERY_TIME, [refused])

    with pytest.raises(DrivineError) as raised:
        await resolve(store, api)

    assert raised.value.root_cause is refused and store.merges == 1


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "refused",
    [
        pytest.param(PostgresUniqueViolation("drivine_node_store_key"), id="postgres-23505"),
        pytest.param(
            OracleFailure("ORA-00001: unique constraint (DRIVINE.DRIVINE_STORE_KEY) violated"), id="oracle-ORA-00001"
        ),
    ],
)
async def test_a_stamp_the_sql_stores_key_constraint_refuses_is_written_again(api: Api, refused: BaseException):
    store = Store(EVERY_TIME, [refused])

    assert await resolve(store, api) == "store-8"
    assert store.merges == 2


@pytest.mark.parametrize("api", APIS)
async def test_a_store_without_schema_management_raises_a_refused_stamp_at_once(api: Api):
    # No constraint of its own can have refused it.
    store = Store(0, [violated()])

    with pytest.raises(DrivineError):
        await resolve(store, api, schema_management=False)

    assert store.merges == 1 and store.creates == 0
