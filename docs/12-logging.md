## Logging

Drivine logs through the standard `logging` module: `drivine.statements` logs each statement at DEBUG, with its
session and how long it took. `DRIVINE_LOG_LEVEL`, as TypeScript's Drivine reads it, sets the level of the `drivine`
logger when the application hasn't.
