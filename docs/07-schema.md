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
- **By hand:** `gom.indexes.ensure(range_index("Session", ["lastActivityAt", "sessionId"]))`, `gom.indexes.recreate`,
  `gom.indexes.drop`, `gom.constraints.ensure(uniqueness_constraint("Person", "email"))`, and `list`.
- **Each engine's DDL** is written for it: named items on Neo4j, Memgraph's text and vector indexes, FalkorDB's index per
  property and its constraint commands, Oracle's tables beside the nodes, and Postgres's partial indexes over a label's
  nodes, recorded in the graph's catalog as drivine4j records them (a pgvector HNSW index for a vector index, a trigram
  index for a full-text one). Neptune has no schema management. Vector index tuning (`hnsw_m`, `hnsw_ef_construction`, and an engine's own
  options) is passed where the engine takes it, and reported where it doesn't.

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
  vector index of another label. Full-text text is the engine's own query language, passed as it is.

### Index advice

An ordered query with no range index over exactly its ordered properties scans and sorts. Drivine says so, once for
each label and properties, naming the index to make. `gom.index_advice = IndexAdvicePolicy.FAIL` raises instead, for
development and CI (or override `index_advice()` on the assembly); `OFF` says nothing. The index list is read once:
`gom.index_advisor.refresh()` after making indexes as the application runs.

### Store identity

`await gom.store_identity()` gives the store's stable id, minted in the store on first sight: two connections see the
same id when, and only when, they look at the same data.
