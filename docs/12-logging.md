## Logging

Drivine logs through the standard `logging` module: `drivine.statements` logs each statement at DEBUG, with its
session and how long it took. `DRIVINE_LOG_LEVEL`, as TypeScript's Drivine reads it, sets the level of the `drivine`
logger when the application hasn't.

Drivine never prints the password. `ConnectionProperties`, including those read from the environment, and the
testing `ContainerConnection` print it as `'***'` through `repr` and `str`. The connection providers print only their
name and type (SQLite's its path too), and they and the builder keep the password in a private attribute.
