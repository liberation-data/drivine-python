## With pi-pilgrim

`DrivineAssembly` is a [pi-pilgrim](https://github.com/liberation-data/pi-pilgrim) assembly holding the registry
and the managers, for your own assembly to use:

```python
from pipilgrim import AssemblyHolder, PilgrimAssembly
from drivine import DrivineAssembly      # drivine.sync's for the sync API


class AppAssembly(PilgrimAssembly):
    def __init__(self):
        self.drivine = AssemblyHolder.shared(DrivineAssembly)
        super().__init__()

    def route_repository(self) -> RouteRepository:
        return self.shared(lambda: RouteRepository(self.drivine.persistence_manager()))

    def report_repository(self) -> ReportRepository:
        return self.shared(lambda: ReportRepository(self.drivine.persistence_manager("analytics")))
```

Open your assembly with `async with`, start Drivine in it, and leave it when the application stops. In FastAPI, that
is the lifespan:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AppAssembly() as assembly:
        await assembly.drivine.start()      # registers the databases and holds the schema
        app.state.assembly = assembly
        yield


app = FastAPI(lifespan=lifespan)
```

- **Closing is pi-pilgrim's.** Leaving `async with` (or `await assembly.aclose()`) closes the assembly's `shared`
  components, newest first, then the `DrivineAssembly` it holds, which closes every database's driver and pool, as
  Spring closes drivine4j's with its context. Your own components close too: whichever of `aclose()`, `close()` or
  `shutdown()` each has, or `destroy=` on `shared`. A closed assembly makes no more components.
- **An assembly closes the ones it asked for first.** `AssemblyHolder.shared(DrivineAssembly)` in your assembly's
  `__init__` makes your assembly its owner. One that something else asked for first is closed by
  `await AssemblyHolder.aclose()`.
- **Entering your assembly doesn't start Drivine**, as entering `DrivineAssembly` itself does: call
  `await assembly.drivine.start()`, as above.
- **The async assembly is an `AsyncPilgrimAssembly`**, as its drivers close asynchronously: `await close()`,
  `asyncio.run(close())`, `await aclose()` and `async with` all close it, and `with` refuses, and says to use
  `async with`. Await `close()`: left unawaited, it closes nothing, and Python warns that a coroutine was never
  awaited. Your assembly closes it whether it holds it with `AssemblyHolder.shared(DrivineAssembly)` or as a component,
  `self.shared(DrivineAssembly)`. The sync assembly, `drivine.sync.DrivineAssembly`, is a plain `PilgrimAssembly`,
  which closes with `with` or `close()`.
- It registers the database the environment describes. To register yours in code, subclass it and override
  `register_databases(registry)`.
- `Assembled` injects a manager into a class something else makes:
  `pm: PersistenceManager = Assembled(assembly=DrivineAssembly)`.

The managers are plain classes, so an application with another container, or none, makes them itself, and closes the
registry with `async with DatabaseRegistry() as registry:`.

### Upgrading from 0.2.0

0.3.0 moved `DrivineAssembly` onto pi-pilgrim 0.3's lifecycle: the assembly closes when you leave `async with`, or when
the application assembly that holds it closes, and closing it closes every database's driver and pool.

- **Shutdown.** 0.2.0 said to call `await assembly.drivine.close()` when the application stops. That closes everything
  again in every release after 0.3.0, where `DrivineAssembly` is pi-pilgrim's `AsyncPilgrimAssembly`, whose `close()`
  is `aclose()` (0.3.0 raised `PilgrimError`, or `TypeError` when Drivine had made no registry). Await it: called
  without `await`, it closes nothing, and Python warns that a coroutine was never awaited. The 0.3 way is to leave your
  assembly's `async with`, as in the lifespan above, or to call `await assembly.aclose()`, which closes your components
  and then Drivine. To close only Drivine, `await assembly.drivine.aclose()`.
- **`with DrivineAssembly()`** raises `AsyncTeardownRequired`, a `PilgrimError`: use `async with`, or
  `drivine.sync.DrivineAssembly` for the sync API, which closes with `with` or `close()`.
- **Sync `close()` on your assembly** closes Drivine while it has made no registry, whether your assembly holds it with
  `AssemblyHolder.shared` or with `self.shared(DrivineAssembly)`. Once Drivine has made its registry it refuses with
  `AsyncTeardownRequired`, before closing anything, since the drivers close asynchronously: close with
  `await aclose()`. It refuses from the start if your subclass of `DrivineAssembly` overrides `aclose()`, which a sync
  close can't run.
