## Sync

`drivine.sync` is the same API without `async` and `await`, for scripts, notebooks, Django and Flask:

```python
from drivine.sync import DrivineAssembly, StatelessGraphObjectManager, transactional


@transactional
def promote(gom: StatelessGraphObjectManager, uuid: str) -> None:
    person = gom.load(Person, uuid)
    gom.save(person.model_copy(update={"bio": "Lead"}))


with DrivineAssembly() as drivine:      # or: with DatabaseRegistry() as registry:
    promote(drivine.graph_object_manager(), "p1")
```

- **Each driver's own sync client**, not an event loop behind the scenes: it costs nothing extra, and works where a loop
  is already running, as in Jupyter.
- **The same models, query DSL and backends.** A class is one class in both APIs; only what does I/O is sync.
- **Each thread has its own transaction**, as asyncio gives each task's context its own.
- `@transactional` from `drivine.sync` takes a plain function, and drivine's takes an async one.

It is written from the async source by `scripts/unasync.py`, as the Neo4j driver writes its sync API, and tested by the
same tests, written alike.
