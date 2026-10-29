## Statements

The `PersistenceManager` runs your statements and maps their results:

```python
from drivine import QuerySpecification

spec = (QuerySpecification[Person]()
    .with_statement("MATCH (p:Person {city: $city}) RETURN properties(p)")
    .bind({"city": "Sydney"})    # named parameters, or a list for $1, $2, …
    .transform(Person)           # validated with Pydantic: a model, dataclass, TypedDict…
    .filter(lambda it: it.age >= 18)
    .limit(10))

people = await pm.query(spec)
person = await pm.get_one(spec)          # exactly one, or it raises
maybe = await pm.maybe_get_one(spec)     # one or None
await pm.execute(spec)                   # no results
```

- A node or relationship comes back as its properties, and Neo4j's dates and times as the standard library's.
- A row of one column is its value, and a row of several is a list.
- A failed statement raises `DrivineError`, which holds the specification, and the database's error as its cause.

### Cypher on Postgres and Oracle

A statement written as Cypher, with `cypher_statement` or `cypher_file`, is compiled to SQL over the graph's tables on
Postgres and Oracle, and gives the rows a graph engine gives, as drivine4j compiles it:

```python
from drivine import QuerySpecification, cypher_statement

spec = QuerySpecification(cypher_statement("MATCH (p:Person {city: $city}) RETURN properties(p)"))
people = await pm.query(spec.bind({"city": "Sydney"}))
```

- A read is a query. A write (`CREATE`, `MERGE`, `SET`, `REMOVE`, `DELETE`) is an anonymous PL/SQL block on Oracle, and
  a function made for the statement in `pg_temp` on Postgres: one statement, applied whole or not at all. A block hands
  its rows back whole, so on Oracle a write is run with `query`, and a cursor over one is refused. On Postgres a cursor
  over a write writes when it is opened, as on a graph engine.
- Parameters are named, `$name`: a statement bound to a list of them is refused.
- A row of one item is its value, and a row of several a list of them. A node is its properties.
- A value is compared as JSON, so `'7'` is not `7`, and a missing property is null. A map keeps its keys in the order
  written. Two lists or maps that may hold a null are compared item by item, as Cypher compares them: `[null] = [null]`
  is null, and `[1, null] = [2, null]` false. `IN` compares the value with each item as `=` does.
- `<`, `<=`, `>` and `>=` order two lists item by item where the statement makes one of them a list (`n.tags < ['b']`).
  Two values whose kind it leaves open, such as two parameters or two properties, compare as null where they hold
  lists. Two maps compare as null, and two that differ do in a list too, where Neo4j orders them.
- A map or a list that holds a node or a relationship (`{x: r}`, `[r, 1]`) compares by what it holds, not by its
  properties, through a `WITH` or a `CALL { … }` too, as on a graph engine. Unwound by `UNWIND`, as a comprehension's
  or a reduce's variable, or carried past a write in the statement, it compares by its properties.
- openCypher's temporal values are their ISO 8601 text, as the store keeps a moment: `date`, `datetime`, `duration` and
  the rest make one, their fields read, and two moments compare as the moments they are. A parameter of a `date`,
  `time`, `datetime` or `timedelta` is a temporal value of its kind. A zoned value the statement makes sorts as its
  moment (`ORDER BY datetime(n.at)`); one read as a property sorts as its text, and so does a map that holds one. A
  list of them, or a list of lists of them, sorts item by item so in an `ORDER BY`, `min` and `max`. Oracle reads by
  their places, not with `JSON_TABLE`, the items of a list that holds temporal values the statement makes, at any
  depth: a list of them, a list of lists or of maps of them (`collect(DISTINCT [l, l])`), one a `CASE`, `coalesce` or
  `reduce` may give, or one taken back out of a map; and the keys of any map, which `keys` reads from a list Oracle
  makes of them. Oracle 23 takes many seconds, or minutes, to parse a `JSON_TABLE` over a list worked out item by item
  with temporal arithmetic (`[i IN range(1, 10) | t + duration({minutes: i})]`), and neither a query timeout nor a kill
  ends the parse ([drivine4j#331](https://github.com/liberation-data/drivine4j/issues/331),
  [drivine4j#347](https://github.com/liberation-data/drivine4j/issues/347)). Other lists keep `JSON_TABLE`, which is
  about ten times as fast for a short list; read by their places, the keys are faster than `JSON_TABLE` gave them.
- `UNION`, `CALL { … }`, `EXISTS { … }`, `COUNT { … }`, `FOREACH`, `=~`, variable-length relationships, `shortestPath`
  and `allShortestPaths` compile, as do quantified relationships (`-[:R]->{1,3}`) and quantified path patterns
  (`(a) ((x)-[r:R]->(y) WHERE x.v < y.v){1,3} (b)`), whose variables are lists of what each repetition matched. On
  Oracle, whose recursive queries read nothing of the row they are in, a quantified path pattern's `WHERE` and
  properties read no variable from outside it, and hold a subquery only where no variable of its `MATCH` or before it
  is named, and that walks no variable-length relationship or quantified path pattern of its own; on Postgres they may.
  What isn't compiled yet (a path returned whole, `SKIP` of an expression…) is
  refused by name before anything is sent, and what isn't Cypher with where it went wrong.
- `CALL db.labels()` and `CALL db.relationshipTypes()` compile, alone or within a query with `YIELD`, and give each
  label and type something has now, read from the nodes and relationships; any other procedure is refused. A parameter
  may be named by a keyword (`$limit`), as in Neo4j. Functions of numbers (`exp`, `log`, `sin`, `atan2`, `round` by a
  mode …) give the floats Neo4j gives, but where Neo4j gives NaN or an infinity (`log(0)`, `sqrt(-1)`, `exp(1000)`) the
  statement fails: JSON holds neither. A float is written as Java writes a double, by `toString` and added to a text:
  `1.0E7`, `1.2E-4`.
- On Postgres, compiled Cypher works float arithmetic, `sum()` and `avg()` out in exact decimal (`numeric`), not as
  doubles, so it is more accurate: `0.1 + 0.2` is `0.3`, where Neo4j gives `0.30000000000000004`. A result
  differs from a graph engine's only around its 16th significant digit, and still comes back as a float. It shows only
  where a result is compared for exact equality with one worked out in doubles, which is fragile with floats anyway. A
  float's text carries no trailing zeros: `toString(4.0 / 2)` is `"2.0"`.
- On Postgres, text is compared and sorted by its code points, as Cypher does (`B, a, b`), not by the database's
  collation: in the `C` collation, whose byte order is code point order in a UTF-8 database, as `initdb` and Postgres's
  images make one. In a list the statement makes, such as `ORDER BY [n.last, n.first]`, a text is ordered by its code
  points over its first 500 characters, and past them by the database's collation. A list read from a property, and a
  map, still order the texts in them by the database's collation (`ORDER BY n.tags`).
- On Oracle a text read out of JSON past 4,000 bytes is read whole where it is measured, cut, changed, searched or
  reversed; an empty text is null; `reduce` doesn't run in a
  statement that writes, in the step of a `reduce`, or where its step reads a path; and `map[key]` reads null where an
  expression gives the key. Two lists ordered with `<`, `<=`, `>` or `>=` are walked item by item by a function the
  query declares too, so such a comparison is refused in a statement that writes and in the step of a `reduce`. Lists or maps that may hold a null are compared item by item, as Cypher compares them:
  where one holds a null, two written as the same JSON are null at once, and any other two are walked by the graph's
  function `<graph>_nulls_equal` (see [Connecting](02-connecting.md#connecting)), in a statement that writes and the step of a `reduce`
  too. Where the store has no such function, a read declares it, and a statement that writes, or the step of a
  `reduce`, compares two such values that differ as JSON as false, so `a = [1]`, of `[null] AS a`, is false there,
  where Cypher's comparison is null. Lists and maps written out are compared item by item everywhere, so
  `[null] = [1]` is null.
- On Oracle a property that a `RETURN` or a `WITH` returns or passes on (`RETURN n.name`) is read through its text,
  which costs about 2.4 to 3.8 times a plain read: 100,000 rows of `RETURN n.name` take about 490 ms, against 150 ms.
  This works around an Oracle bug, seen on 23.26.3: a string past about 32 KB taken from JSON as JSON, returned or fed
  on into more SQL, crashes the session (ORA-03113, with ORA-00600 [17114] or ORA-07445 in the alert log) over two or
  more rows. Only an item that is the property itself is read so: one inside an item (`RETURN [n.name]`,
  `collect(n.name)`) is read as JSON, as before, and isn't worked around. It goes once an Oracle release no longer
  crashes on the repro in [docs/oracle-long-json-string-crash.md](../docs/oracle-long-json-string-crash.md). Such a text
  still can't be sorted by, grouped by or made `DISTINCT`: Oracle sorts and groups JSON only up to about 32 KB, and
  refuses the statement (ORA-40180), leaving the session open.
- Writes are worked around for the same bug: a text that a write takes from an unwound parameter's item, or from
  another value of its rows, to write it (`UNWIND $rows AS r MATCH (n {id: r.id}) SET n.a = r.a`,
  `CREATE (:X {a: r.a})`, `MERGE … SET n.a = r.a`) is read from the row's text, at no cost measured: 1,000 rows set by
  one statement take as long as before. Any other value, a map set whole by `SET n += r` among them, is read as before.
- So are reads of an unwound item's value: a text that a `RETURN` or a `WITH` gives from an unwound parameter's item,
  or from another unwound list's (`UNWIND $rows AS r RETURN r.a`, `WITH r.a AS a`, `SET n.a = r.a RETURN r.a`), is
  read from the item's text, at about 1.4 times a plain read: 100,000 rows of `RETURN r.a` take about 840 ms, against
  600 ms. Any other value is read as before, and so is a value of a map made of a node's properties and not unwound
  (`WITH n {.a} AS m RETURN m.a`); after a write, which carries its rows on, such a value is read from the item's text
  too. A pattern that matches on such a text (`MERGE (n:X {a: r.a})`) compares it as JSON, which Oracle refuses past
  about 32 KB (ORA-40180), leaving the session open.
- On Oracle an `OPTIONAL CALL` or an `OPTIONAL MATCH` is joined `ON 1 = 1`, where Postgres joins `ON TRUE`. This works
  around an Oracle bug, seen on 23.26.3: in a scalar subquery, as a `COUNT { … }` is compiled, a join `ON TRUE` is
  refused (ORA-00907) or crashes the session (ORA-03113, with ORA-07445 in the alert log)
  ([#200](https://github.com/liberation-data/drivine-python/issues/200)). It goes once an Oracle release no longer fails
  the repro in [docs/oracle-on-true-in-scalar-subquery-crash.md](../docs/oracle-on-true-in-scalar-subquery-crash.md).
- On Oracle a compiled query that reads the graph's node or relationship table twice or more, and each query of a
  write that does, carries the hint `NO_JSON_TABLE_TRANSFORM`. This works around an Oracle bug, seen on 23.26.3:
  Oracle's JSON table transformation reads several JSON values of one node's row by one `JSON_TABLE`, and so rewritten
  beside another node's table the query lost that node's label and property tests, giving rows its pattern doesn't
  have and values of the wrong node, or crashing the session (ORA-03113)
  ([#258](https://github.com/liberation-data/drivine-python/issues/258)). A query of one table carries no hint.
- What each statement gives is the [Cypher spec](https://github.com/liberation-data/drivine-cypher-spec), which the
  three Drivine libraries run alike.

The parser runs on ANTLR's runtime, which `drivine[postgres]` and `drivine[oracle]` install.

### Cursors

A cursor streams a large result, reading it from the driver `batch_size` rows at a time:

```python
spec = CursorSpecification[Route]("MATCH (r:Route) RETURN properties(r)").batch_size(500).transform(Route)
async with await pm.open_cursor(spec) as cursor:
    async for route in cursor:
        ...
```

It works in a transaction or outside one, where it has a connection of its own until it is closed.

### Statements in files

```python
from drivine import cypher_file

routes_between = cypher_file(__package__, "routes_between.cypher")
```

Read with `importlib.resources`, so it works from an installed wheel as well as a source tree.
