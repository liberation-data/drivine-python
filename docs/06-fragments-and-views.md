## Fragments and views

Map a node with a `NodeFragment`, and compose fragments and relationships into a `GraphView`, which loads in one
statement:

```python
from typing import Annotated

from drivine import GraphView, NodeFragment, NodeId, Relationship, RelationshipFragment, Root, TargetNode


class Person(NodeFragment, labels=["Person"]):
    uuid: Annotated[str, NodeId()]
    name: str
    bio: str | None = None


class Organization(NodeFragment, labels=["Organization"]):
    uuid: Annotated[str, NodeId()]
    name: str


class WorkHistory(RelationshipFragment):     # a relationship's properties, and the node it leads to
    start_date: str
    role: str
    target: Annotated[Organization, TargetNode()]


class PersonCareer(GraphView):
    person: Annotated[Person, Root()]
    employment_history: Annotated[list[WorkHistory], Relationship("WORKS_FOR")] = []
```

```python
gom = drivine.graph_object_manager()        # from DrivineAssembly, or StatelessGraphObjectManager(pm)

career = await gom.load(PersonCareer, uuid)
leads = await gom.load_all(PersonCareer, "n.bio CONTAINS $text", {"text": "Lead"})   # Cypher over the root, n
total = await gom.count(Person)
```

### Queries

A query is built from a class's **references**, which mirror its fields:

```python
from drivine import any_of, not_, refs

q = refs(PersonCareer)
leads = await gom.load_all(
    PersonCareer,
    where=q.person.bio.contains("Lead"),
    order_by=q.person.name.asc(),
    limit=20,
)

await gom.load_all(PersonCareer, where=[                                 # a list: all must hold
    q.employment_history.target.name.eq("Acme"),                         # across a relationship
    any_of(q.person.name.eq("Ada"), q.person.name.eq("Grace")),
    not_(q.person.bio.is_null()),
])

# one relationship that meets both, rather than each met by any
q.employment_history.any(lambda it: [it.role.eq("Engineer"), it.target.name.eq("Acme")])
q.employment_history.none(lambda it: it.role.eq("Lead"))
```

- **Operators:** `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in_`, `not_in`, `contains`, `starts_with`, `ends_with`,
  `matches`, `contains_ignore_case`, `eq_ignore_case`, `has_item`, `is_null`, `is_not_null`. On a node:
  `instance_of(Type)`, `has_any_label(...)`, and `property(name)` or `predicate(name, operator, value)` for a property
  named at run time by its stored name.
- **Fields go by their Python names**, `q.start_date`, and Drivine writes the stored names. A field the class doesn't
  have fails as the query is written, naming it and the class. `q.x == 1` raises: write `q.x.eq(1)`.
- **Order and pages:** `order_by` takes terms, `asc()` or `desc()`, then `nulls_first()` or `nulls_last()`; `limit`
  and `skip` apply in the database. An order on a list relationship's property sorts that list in each object.
- **Keyset pages:** `seek=[q.released.after(last.released), q.uuid.after(last.uuid)]`, one value for each term of
  `order_by`, ending with a unique key.
- **An order by an expression:** `plus`, `minus`, `times`, `div`, `negate`, `exp`, `floor`, `coalesce`, `case_of`
  with `above`, `below`, `at_least`, `at_most`, `equal_to`, `has_value` and the rest, all computed in the database.
- `count`, `delete`, `delete_all` and `patch` take conditions too.

### Typed references

`refs` is untyped: Python can't derive a reference type for each field from a class, as TypeScript does. A
misspelt field still fails, but when the query is written. To have pyright or mypy check each query, and an editor
complete its fields, write typed references for your models:

```
python -m drivine.stubs myapp.models                 # writes myapp/models_refs.py and models_refs.pyi
python -m drivine.stubs myapp.models --check         # in CI: fails when the models changed since
```

Then import `refs` from the module written:

```python
from myapp.models_refs import refs

q = refs(PersonCareer)                  # PersonCareerRefs
q.person.bio.contains("Lead")           # StringRef
q.person.born.gt("1815")                # error: born is ComparableRef[int]
q.person.nmae                           # error: PersonNodes has no attribute nmae
```

Each field's reference follows from its type: text is a `StringRef`; numbers, dates and times are a
`ComparableRef`; booleans and enums a `ValueRef`; lists and sets a `ListRef`; a property bag gives any entry; and a
relationship leads to the fields of the nodes it reaches, with `any` and `none`. At run time the module is drivine's
`refs`, so nothing changes but what the checker knows. Several modules can share one: `python -m drivine.stubs
myapp.models myapp.billing --out myapp/query_refs.py`.

### The model file

The same step writes `.drivine/model.json` beside your `pyproject.toml`: your fragments, relationship fragments and
views, and how each is stored, in the [format](https://github.com/liberation-data/drivine-cypher-spec/tree/main/model)
drivine4j and Drivine (TypeScript) write too. [Drivine Studio](https://github.com/liberation-data/drivine-studio)
reads it to browse your model and load it, with the Cypher a load runs, without your application running.

It is read from the models ModelRegistry builds of your classes, so it says what a load does: each class's id is its
module and name (`myapp.models.Person`), each field's stored name is the one a save writes (`created_at` as
`createdAt`), and its type is read from its annotation. A class the registry refuses is named, and left out.

Keep the file in source control, as you would a lock file: a change to the model shows in review, and `--check` in CI
fails when it is stale. A class you remove stays in the file, marked `"removed": true`, as the store may still hold
its nodes; delete the entry by hand once it doesn't. The file is written only when it changes. `--no-model`, or
`model = false` under `[tool.drivine]` in `pyproject.toml`, writes none; a module in no project, with no
`pyproject.toml` above it, gets none either.

The sample's file is [`sample/.drivine/model.json`](../sample/.drivine/model.json).

### Grouped reads

Group a fragment's nodes by keys, with numbers about each group, in one statement:

```python
from drivine import avg, count, hop, key, path_key, refs

q = refs(Review)
top = await gom.groups.load(
    Review,
    keys={"page": key(q.page)},                                  # or path_key(Page, [hop("REVIEWS")]): the node
    aggregates={"votes": count(q.helpful.eq(True)), "rating": avg(q.rating)},
    where=q.published.is_not_null(),
    having=[("votes", "gte", 3)],
    order_by=[("votes", "desc")],
    limit=10,
)
# [{"page": "/pricing", "votes": 12, "rating": 4.5}, ...]

await gom.groups.count_by(Review, "page")                       # {"/pricing": 14, ...}
```

Aggregates are `count`, `sum_`, `avg`, `min_` and `max_`, each with an optional filter. Ties in an order are broken by
the keys, so a page of groups is the same every time.

- **Models are Pydantic.** A loaded fragment is validated, so a bad value fails at the load.
- **The annotation says what a relationship holds**: a fragment, a relationship fragment or a view, one or a list. A
  single relationship typed without `None` is required, so only roots that have it load; `Person | None` is
  optional. A list is required with `Relationship(..., required=True)`.
- **Properties are stored in camel case**, as TypeScript and JVM models store them: `start_date` as `startDate`.
  `GraphProperty("name")` names one, and `property_names="as_written"` keeps a class's fields as they are written.
- **A subclass is loaded as itself.** A fragment's labels are inherited, and a node loads as the most specific
  subclass whose labels it carries.
- **Recursion:** `Relationship("HAS_LOCATION", max_depth=3)` on a list of the view's own class expands three levels;
  `depth={"sub_locations": 5}` overrides it for one load.
- **More markers:** `GraphPath([hop(...), ...])` for the nodes at the end of several hops, `Count` and `Aggregate`
  computed in the query, `SortedBy`, `ReadOnly`, `Transient`, `NodeStamp`, `NodeLabels`, `PropertyBag`,
  `JsonPacked`, `NullAsDefault` and `EmptyWhenAbsent`, as in TypeScript's Drivine.
- **A set field** is stored as a list of its items, sorted where they order (as the set iterates where they don't), and
  a `JsonPacked` one packed so: a set is written alike in every process, whatever order it iterates in. `patch` writes
  one so too, and `edges.relate` writes a set property so. One that 0.1.0 wrote, in the order it iterated in, loads and
  `update` saves it, but until a save writes it again a `where` that compares the whole set, which it writes sorted,
  doesn't find it on the graph engines (on Postgres and Oracle a whole-set `where` on a set that isn't `JsonPacked`
  doesn't find it either way), and on Neo4j a save given `from_` of a `JsonPacked` one is refused.
- **A flat `PropertyBag(flat=True)`** stores each entry under its bare key and loads every property no field or other
  bag is stored under. A save refuses, before it writes anything, an entry whose key a field (the id among them) is
  stored under, one under another bag's prefix, or one beginning `__drivine.`, Drivine's own, such as the stamp.

The statements are TypeScript's, character for character: the tests check them against those TypeScript's Drivine
writes for the same models.

### Saving

```python
saved = await gom.save(career)                                       # fields that aren't None; relationships added
await gom.save(career, replace=["employment_history"])               # this list is the whole list
await gom.save(person, only=["name"])                                # or except_=["embedding"]
await gom.save(person, null_policy=NullPolicy.CLEAR)                 # a field assigned None clears its property
await gom.save(edited, from_=loaded)                                 # checked against the object as it was loaded
created = await gom.create(person)                                   # only if absent; None when it was there
updated = await gom.update(Person, uuid, lambda it: it.model_copy(update={"name": "Ada"}))
await gom.delete(PersonCareer, uuid, cascade=CascadeType.DELETE_ORPHAN)
await gom.delete_all(Person, "n.name = $name", {"name": "Ada"})

await gom.save_all(chunks)                                           # fragments in one statement per 1,000
result = await gom.patch(Session, session_id, set={"last_activity_at": now},
                         where="n.lastActivityAt IS NULL OR n.lastActivityAt < $now", bindings={"now": now})
results = await gom.patch_all(chunks, only=["embedding"])            # named fields, where each node is there

await gom.edges.relate(node_ref(Issue, a), node_ref(Person, b), "ASSIGNED_TO")
await gom.edges.unrelate(node_ref(Issue, a), node_ref(Person, b), "ASSIGNED_TO")
people = await gom.edges.load_related(node_ref(Issue, a), "ASSIGNED_TO", Person)
```

- **A save is one statement**, applied whole or not at all: the root, the relationships it drops, each related node
  and the relationship to it.
- **A save adds and removes nothing**, unless a field is named in `replace`. `removed_targets=DELETE_UNREFERENCED`
  also deletes a removed node that nothing else refers to.
- **Stamps refuse a stale save.** On a class with a `NodeStamp` field, a save of an object loaded before another
  writer changed the node raises `StaleObjectError` and writes nothing. Use the object a save returns: it carries the
  new stamp. `update` loads, applies your change and saves what it altered, again if the node changed meanwhile.
- **On Neo4j a load can be handed a stamp newer than its data.** Neo4j reads under read committed, without a snapshot,
  so a load that runs while another writer commits can find that writer's stamp and the data from before its write.
  `update` therefore compares each field of the root as it loaded it too, under the node's lock, and loads again if
  one differs: a field's value (a set field's items, in any order and each once or more, and a `JsonPacked` field's as
  the text the node held), a `PropertyBag`'s keys and their values, and the labels a `NodeLabels` field reads. A
  `save` checks the same when you give it the object as loaded, as `from_`. Without `from_` it has only the stamp to
  go on, and in that moment it can write over the other writer's change: hold on to what you loaded and pass it, or
  change the node with `update`. Where only the stamp makes the round trip, as through a web form, the stamp is all
  there is. Memgraph reads from a snapshot, FalkorDB runs no read beside a write, and Oracle and Postgres read a
  statement whole, so on them the stamp is what was read.
- **`from_` is the object as you loaded it**, when what you save is a changed copy of it. The save is then checked
  against the stamp `from_` carries, whatever stamp the copy carries, as `update` checks against what it loaded, and on
  Neo4j against each field of `from_`'s root too. A `JsonPacked` field is compared as a save packs it: `from_` holds the
  value, not the text the node held, so where another writer packed the same value with other spacing the save is
  refused, and `update`, which compares the text, is the way to change the node. A `from_` of another class, with
  another id, or with no stamp is refused before anything is written, and so is the object saved itself. A copy may
  share an unchanged root with `from_`, as a view copied to change only a relationship does; change the root through a
  copy (`model_copy`), not in place.

  ```python
  loaded = await gom.load(Person, uuid)
  # ... later, from what the user changed:
  await gom.save(loaded.model_copy(update={"email": form.email}), from_=loaded)
  ```
- **`save_all`** runs its statements in one transaction, or in yours, so a failure leaves nothing written. It writes
  the fragments' classes in the order of their names and the fragments of each in the order of their ids, so two
  batches over the same nodes take their locks in the same order. The objects come back in the order given.
- **`patch`** writes named fields of one node by id, if it meets a condition, without loading it, and says whether it
  found the node, applied the patch and changed anything. It never makes a node.
- **Edges** relate and unrelate stored nodes by id, marking both ends' stamps.
- **A cascading delete** of a view follows the relationships a save of it writes, through nested views. It leaves what
  a read-only field loads (a `ReadOnly()` field, a `GraphPath`, and a list of fragments read over several hops): other
  code writes those nodes. `DELETE_ALL` deletes every node the view reaches, and `DELETE_ORPHAN` each one that nothing
  outside the view still needs. That keeps a node something outside the view refers to, at any level, with everything
  the view holds below it: a bookmarked message keeps its attachments, and a pinned sub-session its messages and
  sub-sessions. The rest goes, the session that held a kept message among it. `DELETE_ORPHAN` isn't available on
  Memgraph. A view that contains itself (`parts: list["SessionTree"]`, with a `max_depth`) deletes what a load of it
  reads, every level and what each holds through the relationships a save writes; one that reaches below its
  `max_depth` is refused with a `DrivineError` before anything is written, saying how deep the view reads: raise the
  `max_depth` to cover the deepest level.
- **Outside a transaction**, a save the engine turns away because another writer held the node is run again after a
  pause. Inside one, it joins the transaction, and fails for its owner to decide.
- A save that lost its connection, or whose authorization expired, is not run again, though the Neo4j driver calls that
  retryable: it may have been applied, and a second run would find its own stamp and report a save that was applied as
  refused.

The store format, stamps included, is TypeScript's and drivine4j's, so the three can share a store.
