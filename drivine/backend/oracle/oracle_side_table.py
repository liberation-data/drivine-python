from __future__ import annotations

from collections.abc import Callable

from drivine.backend.oracle.oracle_graph import sql_text


class OracleSideTable:
    """
    A table beside the nodes that holds, for each node of a label, what an Oracle index can be over and a JSON property
    cannot: an embedding as a VECTOR, or the text of some properties as a document (drivine4j's OracleSideTable). A
    trigger on the node table keeps it in step with whatever writes a node, an object manager or SQL by hand; a backfill
    fills it from the nodes there already. Each statement may run again.
    """

    @staticmethod
    def dropping(name: str) -> list[str]:
        """
        Drops the trigger, then the table (the index over it goes with it); neither fails when absent. The table is
        named by `name`, an expression of PL/SQL (the name a catalog's record holds), so each is a statement to execute.
        """
        return [f"'DROP TRIGGER IF EXISTS ' || {name} || '_t'", f"'DROP TABLE IF EXISTS ' || {name} || ' PURGE'"]

    def __init__(
        self,
        name: str,
        nodes: str,
        column: str,
        column_type: str,
        payload: Callable[[str], str],
        indexed: Callable[[str], str],
        backfill_filter: Callable[[str], str] | None = None,
        tolerant: bool = False,
    ) -> None:
        """
        `payload` is the SQL of the payload of the node whose row the argument names, and `indexed` whether that node
        belongs in the table. `backfill_filter` is a further test the backfill makes of a node's payload, which the
        trigger makes by trying to write it. With `tolerant`, a payload the column refuses leaves its node out of the
        table, rather than failing the node's write.
        """
        self.name = name
        self._nodes = nodes
        self._column = column
        self._column_type = column_type
        self._payload = payload
        self._indexed = indexed
        self._backfill_filter = backfill_filter
        self._tolerant = tolerant
        self.trigger = f"{name}_t"
        """The trigger on the node table that keeps this one in step."""
        self.index = f"{name}_i"
        """The index over the column: a vector index, or an Oracle Text one."""

    @property
    def create(self) -> str:
        """Makes the table, when it is not there."""
        return (
            f"CREATE TABLE IF NOT EXISTS {self.name} (nid NUMBER PRIMARY KEY REFERENCES {self._nodes} (nid) "
            f"ON DELETE CASCADE, {self._column} {self._column_type} NOT NULL)"
        )

    @property
    def trigger_ddl(self) -> str:
        """The trigger that keeps the table in step. It names the new row with a colon: see `trigger_statement`."""
        column = self._column
        write = (
            f"MERGE INTO {self.name} s USING (SELECT :NEW.nid AS nid, {self._payload(':NEW')} AS {column} FROM dual) n "
            f"ON (s.nid = n.nid) WHEN MATCHED THEN UPDATE SET s.{column} = n.{column} "
            f"WHEN NOT MATCHED THEN INSERT (nid, {column}) VALUES (n.nid, n.{column});"
        )
        # Not a payload the column takes: the node is kept, and left out of the table, as the graph engines leave it out
        # of the index.
        kept = (
            f"BEGIN {write} EXCEPTION WHEN OTHERS THEN DELETE FROM {self.name} WHERE nid = :NEW.nid; END;"
            if self._tolerant
            else write
        )
        otherwise = f"DELETE FROM {self.name} WHERE nid = :NEW.nid;"
        return (
            f"CREATE OR REPLACE TRIGGER {self.trigger} AFTER INSERT OR UPDATE OF labels, props ON {self._nodes} "
            f"FOR EACH ROW BEGIN IF {self._indexed(':NEW')} THEN {kept} ELSE {otherwise} END IF; END;"
        )

    @property
    def trigger_statement(self) -> str:
        """Makes the trigger from inside a block, so that the `:NEW` it names is not read as a parameter."""
        return f"BEGIN EXECUTE IMMEDIATE {sql_text(self.trigger_ddl)}; END;"

    @property
    def backfill(self) -> str:
        """Fills the table from the nodes that are there and not in it yet."""
        further = f" AND {self._backfill_filter('n')}" if self._backfill_filter else ""
        return (
            f"INSERT INTO {self.name} (nid, {self._column}) SELECT n.nid, {self._payload('n')} FROM {self._nodes} n "
            f"WHERE {self._indexed('n')}{further} "
            f"AND NOT EXISTS (SELECT 1 FROM {self.name} s WHERE s.nid = n.nid)"
        )
