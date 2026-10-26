"""The sync API's graph tables: made once for threads whose connections ask at once."""

import threading
import time

from drivine.sync.connection.graph_tables import GraphTables


def test_makes_the_tables_once_for_threads_that_ask_at_once():
    tables, statements = GraphTables("MAKE TABLES"), []
    started = threading.Barrier(5)

    def run(statement: str) -> None:
        statements.append(statement)
        # Takes a while, as a statement sent to a database does, so the other threads ask meanwhile.
        time.sleep(0.05)

    def connect() -> None:
        started.wait()
        tables.ensure(run)

    threads = [threading.Thread(target=connect) for _ in range(5)]
    for it in threads:
        it.start()
    for it in threads:
        it.join()
    assert statements == ["MAKE TABLES"]
