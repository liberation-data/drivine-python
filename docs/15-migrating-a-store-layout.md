## Migrating a graph from store layout 1 to layout 2

drivine-python 0.9.0 holds a graph on Postgres, Oracle and SQLite in **store layout 2**:
- a temporal value is held as a tagged map of its ISO 8601 text and the key it orders by, so it compares and sorts as its kind;
- a float stays a float;
- a property whose name begins with `$` is held under one more `$`.

Every drivine-python before 0.9.0, drivine4j before 0.8.0 and Drivine for TypeScript before 6 wrote **layout 1**. Layout 1 held each value as the store's JSON alone: a date as its text, a whole-number float as a whole number.

A graph that still holds your nodes in layout 1 is refused on its first connection, with a `StoreLayoutError` that names the graph (its `graph`). There are two ways forward:
- **Export and import.** Export the graph with the Drivine version that wrote it, and import it with this one.
- **Migrate it in place**, as this page describes. `StoreLayoutMigration` rewrites every row once, in batches, and can be stopped and resumed at any point.

Neo4j, Memgraph and FalkorDB hold values natively and have no store layout: nothing on this page applies to them.

### Which graphs it migrates

It migrates graphs on Postgres, Oracle and SQLite written by drivine-python 0.8.0, drivine4j 0.7.0 or Drivine for TypeScript 5.0.0, the last releases of layout 1. Each library's tests write a graph with its own last layout-1 release and migrate it, on each store. A graph an earlier release wrote is in the same layout and is migrated the same way, but those releases aren't tested.

The shared compiler decides every step, so all three libraries migrate a graph alike, into the layout 2 each of them reads.

### Before you start

1. **Stop everything that writes to the graph.** A migration runs while nothing else uses the graph. A resume refuses a graph whose size or schema marker changed since the migration began, and a write made during the run of a value it didn't expect stops it as `invalid`.
2. **Take a backup.** The migration has no down-migration: a graph changed under it, or one you want back as it was, is restored from the backup.
   - **Postgres:** `pg_dump -t '<graph>_*'`.
   - **Oracle:** Data Pump (`expdp`) of the graph's tables.
   - **SQLite:** a copy of the file, taken while nothing has it open, or `sqlite3 <file> ".backup <copy>"`.
3. **Give it the provider.** It runs on connections of its own from the provider's pool, never one of an application's transaction. On Postgres and Oracle it takes two at once (one holds the lock), so the pool needs room for both.

### 1. Declare what layout 1 can't tell

Layout 1 can't tell which texts are dates or moments, which whole numbers are floats, and which lists of numbers or texts are bytes. Tell the migration, label by label and property by property:

```python
from drivine.migration import Declaration, StoreLayoutMigration, declarations_of, with_declared

declarations = with_declared(
    [*declarations_of(Person), *declarations_of(Knows, "KNOWS")],
    [Declaration("Event", "at", "datetime")],
)
```

`declarations_of(Person)` reads a node fragment's fields of type `date`, `datetime` (naive or aware) or `AwareDatetime`, `timedelta` or `float`, or a list, tuple or set of one. It does so for each of the fragment's labels, inherited ones too, by the name each field is stored under. Pass `registry=` the `ModelRegistry` your managers are given, where it names properties otherwise than in camel case. `declarations_of(Knows, "KNOWS")` does the same for a relationship fragment. `with_declared(derived, by_hand)` replaces or adds declarations by hand.

Some fields 0.8.0 saved in another form than their kind's, and these get no declaration, so they are kept as they are:
- a `NaiveDatetime`, saved as a UTC moment with a `Z`, not as a local datetime;
- a `time`, saved with its offset where it had one, which its type can't tell;
- `bytes`, saved as their UTF-8 text;
- a `JsonPacked` field, saved as JSON text.

A property nobody declares is kept as layout 1 held it: a date-looking text stays a text.

### 2. Dry run

```python
migration = StoreLayoutMigration(registry.connection_provider("graph"))
report = await migration.migrate(declarations, dry_run=True)
```

`drivine.sync.migration` gives the same, without `await`.

A dry run writes nothing. Its report, a `MigrationReport` dict, gives:

- **`tables`:** the rows of nodes and of relationships, and how many would change.
- **`properties`:** for each label and property a rewrite changes, the count of each change, and each value that is no value of its declared kind, with why.
- **`candidates`:** properties you didn't declare whose values all read as one kind. Declare them if they are that kind. They are never applied otherwise.
- **`collisions`:** the uniqueness constraints' keys that may collide.

**What a dry run's collisions cover.** A dry run checks each uniqueness constraint over the values as layout 1 holds them. Some keys are distinct in layout 1 but one value in layout 2, and the dry run doesn't report those. For example, two spellings of one moment, `2024-03-15T10:30:00.5Z` and `2024-03-15T10:30:00.500Z`, declared `datetime` under a unique key. Such a collision is found after the writes: the migration stops at `collisions`, and once corrected it resumes (see [Collisions](#collisions)). Checking rewritten values in the dry run is tracked in [drivine-cypher-spec#429](https://github.com/liberation-data/drivine-cypher-spec/issues/429).

### 3. Migrate

```python
report = await migration.migrate(declarations)
print(report["tables"] if report["status"] == "migrated" else f"{report['status']}: {report['message']}")
```

The migration drops layout 1's indexes first, then reads and rewrites the nodes, then the relationships, 1000 rows at a time (`batch_size`). Each batch's rows are written with the migration's checkpoint in one transaction, so each row is rewritten exactly once. Then it checks the uniqueness constraints, stamps the graph layout 2 and drops its checkpoint.

How a migration ends (`report["status"]`):

| Status | Meaning | What to do |
|---|---|---|
| `migrated` | The graph is in layout 2. | Start the application (see [After](#after-the-migration)). |
| `current` | The graph is in layout 2 already. | Nothing. |
| `empty` | The graph holds no nodes of yours. | Nothing. A first connection stamps it. |
| `dryRun` | A dry run's report. Nothing was written. | Read it, then migrate. |
| `invalid` | A declared property holds a value that isn't of its kind. Nothing was written, unless the value was written to the graph during the migration: then the batches before its own are written, and the graph keeps its checkpoint. | Declare it otherwise, correct the value, or keep such values (below). |
| `collisions` | Keys a uniqueness constraint holds are one value in layout 2. Every row is written, and the graph keeps its checkpoint. | Correct the rows, and run it again. |
| `inProgress` | A dry run of a graph part way through its migration. | Run the migration again to resume it. |
| `refused` | A graph of another layout, one changed since its migration began, a resume with other declarations, or another migration running. | Read the message. |
| `failed` | A statement failed, and the message is the store's. | Fix the cause, and run it again to resume. |

**Values not of their kind.** Pass `keep_invalid=True` to keep each such value as it is, named in the report, rather than refuse the graph. A dry run with `keep_invalid=True` lists them all first. Resume a migration with the same `keep_invalid` it began with.

### Collisions

The `collisions` report lists each constraint's label and properties, with a sample of the keys that collide. Correct the rows with the store's own tools, keeping their values as layout 2 holds them: delete the duplicate, or change its key. Then run the migration again. The rows are written already, so it checks the constraints again and finishes.

### Stopping and resuming

A migration stopped at any moment can be resumed: by `failed`, by `collisions`, by a killed session, or by the process ending. Run `migrate` again with the same declarations. A resume carries on after the last batch committed, and never rewrites a row twice.

Until every row is written, a resume is refused in these cases:
- the declarations or `keep_invalid` differ from those the migration began with;
- the graph's size, stamp or schema marker changed since it began, which means something wrote to it.

Restore such a graph from the backup.

While a migration is part way through, the graph is refused to the application's connections, and a dry run of it reports `inProgress`.

### After the migration

- **Indexes and constraints:** the migration makes none. It drops layout 1's, which index values as layout 1 held them. Your application's schema plan makes the ones it declares at its next start, as it does on a new store (see [Schema](07-schema.md)). Until then a query that would read one scans, and no uniqueness constraint is enforced, so start the application, or ensure its schema, before anything else writes.
- **The count store** (`<graph>_count2`) is left empty, as a new store has it. Nothing reads it in this release ([drivine-cypher-spec#349](https://github.com/liberation-data/drivine-cypher-spec/issues/349)).
- **The rest is unchanged:** the graph's nodes and relationships keep their ids, and its stamp, its `DrivineStore` node, is kept and given layout 2.
