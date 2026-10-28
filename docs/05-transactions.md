## Transactions

```python
from drivine import transactional


class IssueService:
    def __init__(self, issues: IssueRepository, audit: AuditLog):
        self.issues = issues
        self.audit = audit

    @transactional()           # join the current transaction, or start one
    async def raise_issue(self, issue: Issue) -> Issue:
        saved = await self.issues.save(issue)
        await self.audit.record(saved)   # same transaction
        return saved
```

Or for a block:

```python
from drivine import transaction

async with transaction():
    await pm.execute(first)
    await pm.execute(second)

async with transaction(rollback=True):   # a test that leaves nothing behind
    ...
```

- **The outermost call or block owns the transaction.** It commits when it ends, and rolls back when an exception
  leaves it. Inner ones join it.
- **A failure anywhere rolls the whole transaction back.** An exception that leaves any `@transactional` call, even
  one the caller catches, marks the transaction rollback-only. It then rolls back, and its end raises
  `TransactionRolledBackError` rather than committing what the inner call half did. `no_rollback_for=(NotFound,)`
  exempts an exception.
- **One transaction spans every database it touches.** It opens a connection to each, and begins its transaction,
  the first time it runs a statement there. Commits are one after another, not two-phase.
- **A `@transactional` method that never reaches Drivine costs nothing**: no connection is opened. Code with fakes in
  its tests, or with Drivine taken out, runs as written.
- **Outside a transaction**, each statement runs on its own and commits. Drivine never starts a transaction you didn't
  ask for.
- **It works on calls to `self`**, since it wraps the function itself, not a proxy.

### FalkorDB

FalkorDB has no multi-statement transactions: each statement commits as it runs. With `DATABASE_TRANSACTION_MODE=WARN`,
the default, a transaction runs statement by statement and a rollback logs that nothing was rolled back; a `save_all`
of several statements does the same. With `STRICT`, a transaction is refused at its first statement, and such a batch
raises `NonAtomicBatchError`, so no code path can rely on atomicity it doesn't have.

### Tasks share their transaction, one statement at a time

The current transaction lives in a context variable, so asyncio carries it across every `await` and into the tasks
started inside it:

```python
@transactional()
async def import_all(self, people: list[Person]) -> None:
    async with asyncio.TaskGroup() as tasks:
        for person in people:
            tasks.create_task(self.people.add(person))   # all in this transaction
```

A database transaction runs one statement at a time, so Drivine queues them, in the order they were issued. A
cursor takes its turn for each row it reads, so other statements run between its rows. A statement that fails dooms
the transaction, and the statements after it raise `TransactionRolledBackError` at once.

The queue keeps concurrency correct, not parallel. For a parallel import, give each task its own transaction.
