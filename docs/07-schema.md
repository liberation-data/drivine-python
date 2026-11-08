## Schema

Declare indexes and constraints on the fragments, and hold them on every database at start-up:

```python
class Chunk(NodeFragment, labels=["Chunk"], schema=[RangeIndex(["section_id", "position"])]):
    uuid: Annotated[str, NodeId(), Unique()]
    section_id: str
    position: int
    text: Annotated[str, FullTextIndex()]
    embedding: Annotated[list[float] | None, VectorIndex(similarity="COSINE")] = None


class AppDrivine(DrivineAssembly):
    def schema_options(self) -> SchemaOptions:
        return SchemaOptions([SchemaCatalog.from_fragments(Chunk, dimensions=lambda label, prop: 1536)])

async with AppDrivine() as drivine:      # makes what is missing on the way in; idempotent
    ...
```

`RangeIndex(ignore_case=True)` and `Unique(ignore_case=True)` on a `CaseInsensitive` field are made over the lowercased
copy a save keeps of it, so they serve `eq_ignore_case` and refuse two values that differ only in case. Ensuring one
fills the copies in first. See [Lookups that ignore case](06-fragments-and-views.md#lookups-that-ignore-case). The
object manager keeps the copy in step, but Cypher you send yourself doesn't: set the copy in the same statement, or
`recreate` the index, or `drop` and `ensure` the constraint, afterwards to fill it in again
([how](06-fragments-and-views.md#cypher-of-your-own)).

Entering the assembly calls `start()`, which you may call yourself instead: `await drivine.start()`, from your
application assembly, say. It registers the databases whether or not there are catalogs, as drivine4j does, so a
missing `DATABASE_TYPE` raises `DrivineError` there rather than on first use. Without the assembly, enforce it yourself:
`await SchemaManager(registry, factory, options).enforce()`.

- **`ensure` is idempotent.** An item that is there is left; one there with another shape (a vector index's dimensions,
  say) is reported as DRIFT and left, unless `recreate_on_drift`. A uniqueness constraint the data already breaks is a
  VIOLATION, with a sample of the keys that repeat: each its `properties`, the `count` of nodes that hold it, and
  `nodes`, up to the sample size of them, each named by the engine's id (`elementId`) and, for a constraint read from
  a fragment, its `NodeId` (`uniqueness_constraint(..., node_id_property="uuid")` by hand). This is a breaking change
  from 0.1, where each entry was the repeated value itself: it is now a `ViolatingKey`, the value in its `properties`.
  A sample size of 0 skips reading the data first, and the engine's refusal of a constraint the data breaks is raised.
  FalkorDB doesn't refuse one: it builds it in the background and lists the build as FAILED. Drivine then drops the
  failed constraint, which enforces nothing, and reads the data: a key held more than once is a VIOLATION, as in
  drivine4j, with an empty sample at sample size 0, and a build that fails with no key held more than once raises.
  Once the data is clean, `ensure` makes the constraint. A constraint FalkorDB already lists as FAILED, or still
  UNDER CONSTRUCTION (another process's build, or an earlier run's), is not taken as ALREADY_MATCHING: one under
  construction is waited on, and matches once built, and a failed one is dropped and made again. `drop` also removes a
  constraint FalkorDB failed to build.
  `mode="FAIL_FAST"`, the default, raises on either; `"WARN"` logs.
- **Versions.** `catalog.with_version("2")` recreates the catalog's items once, the first time it is enforced. Items an
  owner applied before and no longer declares are reported, never dropped. Each owner's version and inventory are kept
  in a `_DrivineSchema` node, as drivine4j and TypeScript keep them.
- **Schema known only at runtime**, a vector index whose dimensions come with the embedding model or an index for each
  label a tenant declares, is given to the manager under an owner: `await schema_manager.ensure("dice",
  vector_index("Proposition", "embedding", 1536))`, or `ensure_catalog(catalog)` for a catalog naming its owner and
  databases. The items join what the owner declared, for the life of the manager, and are applied under the same
  options; a later `enforce` or `recreate_all` applies them with the rest. A declaration that conflicts with another
  owner's, or the owner's own, raises, and nothing is kept or applied. The marker records them as ensured at runtime
  (`runtime:` before the key, as drivine4j writes it), so the next process's `enforce` keeps them rather than calling
  them orphans; when it recreates the owner (on a version change, under `recreate_on_startup`, or by `recreate_all`), it
  marks them (`runtime-stale:`), and the next `ensure` recreates them.
  A version change is left to `enforce`.
- **Catalogs are read once**, from the options the manager is built with; a catalog made later isn't seen, and schema
  known later is given to `ensure`. With nothing to apply, no catalog or only empty ones and nothing ensured, the
  manager says so at INFO: that it found no `SchemaCatalog`, or that all it found are empty, naming their owners. So a
  missing catalog, and one left empty until an embedding model exists, can be told from an application with no schema.
- **A vector field needs its `VectorIndex`.** Drivine knows a field is an embedding from the marker alone: without it
  the field is saved as a plain list, not as a vector FalkorDB's index serves, and `load_nearest` can't find the
  index. So a catalog's `vector_index` over a fragment's field without one is refused with a `DrivineError` naming
  the class, the field and the spec, whether the catalog is enforced first or the fragment's model is built first.
  One given to `ensure` is refused the same way, before it is kept or applied.
  A spec beside the marker is allowed (it tunes the index), as is one on a label no fragment maps.
- **An index in the way.** Neo4j refuses a uniqueness constraint while a plain range index covers the same label and
  properties. `ensure` reports that as BLOCKED_BY_INDEX, naming the index, and changes nothing: `FAIL_FAST` raises,
  `WARN` logs. With `replace_blocking_index=True` (on `gom.constraints.ensure`, or in `SchemaOptions`) the data is
  read, and unless a value repeats the index is dropped and the constraint made, its own index taking the plain one's
  place. A constraint given to `schema_manager.ensure` is reported and replaced the same way. Drivine's own keys (the
  schema marker's, the store's) always replace one. On FalkorDB the constraint needs the range index, and Memgraph,
  Oracle and Postgres make it beside one, so none is ever reported in the way there.
- **Taking items back.** `await schema_manager.remove("dice", spec)`, or `remove_catalog(catalog)` to name databases,
  is the counterpart of `ensure`. Each item is found by its kind, label and properties, in any order, whatever its
  name. It is forgotten among what `ensure` gave the owner, so a later `enforce` doesn't make it again, and taken out of
  the owner's marker. It is dropped, unless a startup catalog (of any owner) or another owner's runtime items declare
  it, or another owner's `_DrivineSchema` marker in the database records it, whichever manager or process wrote it:
  then it is left in place, with a warning naming who declares it. An item that isn't there is forgotten all the same.
- **By hand:** `gom.indexes.ensure(range_index("Session", ["lastActivityAt", "sessionId"]))`, `gom.indexes.recreate`,
  `gom.indexes.drop`, `gom.constraints.ensure(uniqueness_constraint("Person", "email"))`, `gom.constraints.recreate`,
  and `list`.
- **Each engine's DDL** is written for it: named items on Neo4j, Memgraph's text and vector indexes, FalkorDB's index per
  property and its constraint commands, Oracle's tables beside the nodes, and Postgres's partial indexes over a label's
  nodes, recorded in the graph's catalog as drivine4j records them (a pgvector HNSW index for a vector index, a trigram
  index for a full-text one). Neptune has no schema management. Vector index tuning (`hnsw_m`, `hnsw_ef_construction`, and an engine's own
  options) is passed where the engine takes it, and reported where it doesn't.

### Neo4j schema DDL on the SQL stores

Postgres, Oracle and SQLite run Neo4j's schema DDL, given as Cypher (`cypher_statement(...)`) to a
`PersistenceManager`, so that a schema file written for Neo4j, such as an APOC export's, replays as it is. Drivine reads
each statement and applies it through the schema machinery, as the schema API applies a spec: the item it makes has the
name, the expressions and the inventory entry the API gives the same spec, so a later `enforce` of an application that
declares it finds it matching, not drifted or orphaned. The statement gives no rows. Neo4j, Memgraph and FalkorDB are
sent the statement as it is. DDL sent as SQL is SQL, and runs as it is.

| DDL | Applied as |
|---|---|
| `CREATE [RANGE] INDEX [name] [IF NOT EXISTS] FOR (n:L) ON (n.a, n.b)`, or Neo4j 4's `ON :L(a, b)` | `range_index("L", ["a", "b"])` |
| `CREATE CONSTRAINT [name] [IF NOT EXISTS] FOR (n:L) REQUIRE n.p IS [NODE] UNIQUE`, `REQUIRE (n.a, n.b) IS UNIQUE`, or Neo4j 4's `ON (n:L) ASSERT n.p IS UNIQUE` | `uniqueness_constraint` |
| `CREATE FULLTEXT INDEX … FOR (n:L) ON EACH [n.a, …]` | `full_text_index`; its `fulltext.analyzer` is read, and not applied yet ([#442](https://github.com/liberation-data/drivine-python/issues/442)), and `fulltext.eventually_consistent` is ignored |
| `CREATE VECTOR INDEX … FOR (n:L) ON n.p OPTIONS {indexConfig: {…}}` | `vector_index`: `vector.dimensions` (required), `vector.similarity_function` (`cosine` or `euclidean`, in any case), `vector.hnsw.m` and `vector.hnsw.ef_construction`, and `vector.quantization.enabled` as Neo4j's `EngineVectorOptions`; `indexProvider` is ignored |
| `CREATE TEXT INDEX …` | Neo4j's TEXT index, which the SQL stores don't make, so the statement fails |
| `DROP INDEX name [IF EXISTS]`, `DROP CONSTRAINT name [IF EXISTS]` | `SchemaManager.remove` of the item that goes by the name |
| `CALL db.awaitIndexes(…)`, `CALL db.awaitIndex(…)` | Nothing: an index on a SQL store is whole once it is made |

A statement is read as DDL only where it opens with one of these keyword sequences, so Cypher that merely uses the words,
such as `CREATE index = (a)-[:R]->(b)`, is Cypher. Names, labels and properties may be in backticks, with spaces, and a
doubled backtick for one. Keywords are read in any case, and a statement may end with `;`.

**Refused by name.** POINT and LOOKUP indexes; an index or constraint on relationships; one over several labels
(`(n:A|B)`); existence (`IS NOT NULL`), key (`IS NODE KEY`, `IS KEY`) and type (`IS :: TYPE`) constraints; a vector index
over several properties, with `WITH [...]` properties, or without `vector.dimensions`; Neo4j 4's `DROP … ON` by label
and properties; a name, or `OPTIONS`, given as a parameter. The message says what is not made and why, and the Cypher
compiler refuses the statement with the same message. One a SQL store honours is refused by the compiler too, as a
schema command to run through a `PersistenceManager`. The Cypher spec's `schema.yaml` pins how each statement reads, in
all three libraries.

**The `ddl` owner.** What DDL creates is ensured with `SchemaManager.ensure` under an owner of its own, `ddl`, never an
application's: an application's own owner records what its catalogs declare, and an ensure of DDL under it would take
the place of that. So the items are recorded in the `_DrivineSchema` marker of scope `ddl`, as ensured at runtime, and an
application that declares the same item co-owns it. A `DROP` removes the item from the `ddl` owner and drops it, as Neo4j
does, unless another owner records it: then it is left in place, with a warning, and no longer goes by the name
dropped, so the application's next `enforce` finds it as it was. The marker's and the store stamp's own constraints
(`_DrivineSchema(scope)`, `DrivineStore(key)`), which an export of a Drivine graph carries, are ensured as Drivine
ensures them and recorded in no inventory; any other schema for those labels is refused, and a `DROP` of either too.

**Names and aliases.** The item is made under the name the schema API gives it (`Label_property_unique`, …). The DDL's
name, where the item goes by another, is kept as an alias in the item's record in `<graph>_schema`, a list `aliases`
among its `details`, as drivine4j keeps it, so that a `DROP` by it finds the item, in this process or a later one: an
APOC export's `DROP CONSTRAINT UNIQUE_IMPORT_NAME`, its last statement, drops the constraint its schema file named so.
A `DROP` looks the name up as the API's name first, then as an alias. A recreate (drift, a version change,
`recreate_all`) carries an item's aliases over. Over an item of the same properties in another order, the DDL's spec is
ensured as that item, which goes by its own name, so the DDL's name, or the API's name for its spec, is kept as an
alias of it.

`CREATE` follows Neo4j: over an item of the same kind, label and properties that is there already, `IF NOT EXISTS` does
nothing but add the alias (and leaves an item of another shape as it is, with a warning), and without it the statement
fails. A name another item goes by, by the API's name or an alias, is refused, with `IF NOT EXISTS` or without. A
`DROP` of a name no item goes by fails, and does nothing with `IF EXISTS`; `DROP INDEX` of a constraint's name fails, as
does `DROP CONSTRAINT` of an index's.

**Transactions.** DDL is refused inside an open transaction (the transactional manager, or the delegating one inside
`transaction()` or `@transactional()`), save `db.awaitIndexes`, which does nothing there: Drivine applies schema on
connections of its own, committed as they go, which the transaction's rollback would not undo. Run it outside the
transaction.

### Search

A fragment's `VectorIndex` field drives `load_nearest`, and its `FullTextIndex` drives `load_matching`. Either takes a
fragment or a view, searched by its root, and gives each hit with its score, best first:

```python
hits = await gom.load_nearest(Chunk, embedding, top_k=5, where=refs(Chunk).section_id.eq(section))
for hit in hits:
    print(hit.score, hit.value.text)

found = await gom.load_matching(Chunk, "graph AND database", top_k=10, threshold=0.5)
```

- **One scale on every engine.** A nearest score is a similarity, higher nearer: (1 + cos) / 2 for cosine, 1 / (1 + d²)
  for euclidean. A matching score is scaled so that the best hit is 1: the best with every label of the root, as the
  index is over the first.
- **Memgraph's cosine scores are read from its distance**, not its similarity, which is the absolute value of
  1 - distance (memgraph/memgraph#5199). Earlier releases read the similarity, so a vector more than 90 degrees from
  the query scored as its mirror image does: a cosine of -0.6 as 0.8, not 0.2. Those scores, and the hits a cosine
  `threshold` keeps, change; positive cosines and euclidean scores don't.
- **Full-text scores on Postgres, SQLite and Oracle without Oracle Text changed after 0.3.2.** A hit scored by how
  often the query's terms occur in it. It now scores by BM25, as Neo4j's index scores it (see
  [Postgres](02-connecting.md#postgres)): a rare term outweighs a common one, and a short text outranks a long one that
  holds a term as often. A term written again at once, `kiwi kiwi kiwi`, is counted each time, where it was counted
  every other time. A `prefix*` adds the same to every hit that holds it, as on Neo4j. The hits can come in another
  order, and a `threshold` can keep others.
- **A query's words on Postgres, SQLite and Oracle changed after 0.3.2.** A word was a run of what Python's `\w`
  holds. It is now one of what the text's `[[:alnum:]_]` holds (see [Postgres](02-connecting.md#postgres)): a Hindi
  word is no longer split at its vowel sign, and `m²x` is the phrase `m x`, as Neo4j's analyzer reads it.
- **Filters apply after the search:** `threshold`, `where`, and a view's required relationships. So a search can return
  fewer than `top_k`; `search_k` asks the index for more, and `top_k` trims after the filter.
- **A full-text search is bounded where the index yields.** A matching score is scaled over every hit the index
  yields, so on Neo4j, Memgraph and FalkorDB `top_k` bounds the hits too, and an ordinary query that matches most of
  a corpus costs no more than them. The index is asked for the best `top_k` hits. Where something filters them, it is
  asked for `top_k × 10` (`FILTERED_OVER_FETCH`), and the filter keeps the best `top_k` of those. Filters are a
  `where`, a view's required relationships and paths, and a label beyond the first (the index is over the first); a
  threshold keeps the best hits, so it isn't one. So a filter that passes fewer than one in ten of the best hits can
  return fewer than `top_k` rows while the index holds more that pass it. The index is asked for no more than 2,147,483,647 hits, and Memgraph's for no more than
  1,000,000, however large `top_k` is.
- `property` names the field to search when a fragment declares more than one index; `partition_label` searches the
  vector index of another label. Full-text text is the engine's own query language, passed as it is; Postgres,
  SQLite and Oracle read the core of Lucene's, as Neo4j does, so in a `"phrase"` a `*` is text, not a `prefix*`.

### Index advice

An ordered query with no range index over exactly its ordered properties scans and sorts. Drivine says so, once for
each label and properties, naming the index to make. `gom.index_advice = IndexAdvicePolicy.FAIL` raises instead, for
development and CI (or override `index_advice()` on the assembly); `OFF` says nothing. The index list is read once:
`gom.index_advisor.refresh()` after making indexes as the application runs.

### Store identity

`await gom.store_identity()` gives the store's stable id, minted in the store on first sight: two connections see the
same id when, and only when, they look at the same data.
