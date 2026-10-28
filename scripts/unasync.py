"""
Writes drivine.sync from the async source, as the Neo4j driver writes its sync API: the same code, without `async` and
`await`, on each driver's sync client. Integration tests get the same treatment, so the sync API is tested as the async
one is.

    uv run python scripts/unasync.py           # writes drivine/sync and tests/sync_integration
    uv run python scripts/unasync.py --check   # writes nothing; fails when they are out of date, for CI

A module is written again when it has async code, or imports one that does; the rest (models, the query DSL, the
backends) is shared by both APIs. drivine/concurrency.py, what the async API asks of asyncio, has a counterpart written
by hand: drivine/sync/concurrency.py. A test marked `async_only` (one that runs tasks concurrently) is left out, as
are the definitions ASYNC_ONLY names.
"""

from __future__ import annotations

import argparse
import ast
import filecmp
import io
import shutil
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Tree:
    """A package written again: its source, where its sync code goes, and what is never written."""

    source: str
    target: str
    skipped: tuple[str, ...] = ()


TREES = (
    Tree("drivine", "drivine.sync", ("drivine.sync", "drivine.testing", "drivine.stubs")),
    Tree("tests.integration", "tests.sync_integration"),
)

TWINS = {"drivine.concurrency": "drivine.sync.concurrency"}
"""Modules whose sync counterpart is written by hand."""

EXTERNAL = {"falkordb.asyncio": "falkordb"}
"""Driver modules whose sync counterpart is another module."""

ASYNC_ONLY = {
    "drivine.drivine_assembly": (
        "_ASYNC_TEARDOWN_REFUSAL",
        "_Closed",
        "DrivineAssembly.close",
        "DrivineAssembly.__enter__",
        "DrivineAssembly.__exit__",
    ),
}
"""
Definitions, and assignments to a name, left out of the sync code, by module: the async API's own, which the sync API
has from pi-pilgrim.
"""

RENAMES = {
    "AsyncIterator": "Iterator",
    "AsyncIterable": "Iterable",
    "AsyncGenerator": "Generator",
    "asynccontextmanager": "contextmanager",
    "__aenter__": "__enter__",
    "__aexit__": "__exit__",
    "__aiter__": "__iter__",
    "__anext__": "__next__",
    "StopAsyncIteration": "StopIteration",
    "aclose": "close",
    "anext": "next",
    "aiter": "iter",
    "AsyncGraphDatabase": "GraphDatabase",
    "AsyncDriver": "Driver",
    "AsyncSession": "Session",
    "AsyncTransaction": "Transaction",
    "AsyncManagedTransaction": "ManagedTransaction",
    "AsyncResult": "Result",
    "create_pool_async": "create_pool",
    "connect_async": "connect",
    "AsyncConnectionPool": "ConnectionPool",
    "AsyncConnection": "Connection",
    "AsyncCursor": "Cursor",
    "AsyncIteratorFixture": "IteratorFixture",
}
"""Names whose sync counterpart is another name: the language's, and each driver's."""

UNWRAPPED = ("Awaitable",)
"""Generic types whose sync counterpart is their argument: `Awaitable[R]` is `R`."""

STRINGS = {"await ": "", "async with ": "with ", "async for ": "for ", "async def ": "def "}
"""Edits made to strings, so a docstring's example reads as sync code."""

LEFTOVERS = {"async", "await", "asyncio"} | set(RENAMES) | set(UNWRAPPED)


def module_of(path: Path) -> str:
    parts = path.relative_to(ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def tree_of(module: str) -> Tree | None:
    for tree in TREES:
        if (module == tree.source or module.startswith(f"{tree.source}.")) and not any(
            module == it or module.startswith(f"{it}.") for it in tree.skipped
        ):
            return tree
    return None


def sync_name(module: str) -> str:
    tree = tree_of(module)
    assert tree is not None
    return tree.target + module[len(tree.source) :]


def is_async(source: ast.Module) -> bool:
    return any(
        isinstance(it, (ast.AsyncFunctionDef, ast.Await, ast.AsyncWith, ast.AsyncFor))
        or (isinstance(it, ast.comprehension) and it.is_async)
        for it in ast.walk(source)
    )


def imported(source: ast.Module, modules: set[str]) -> set[str]:
    """The modules of the package a module imports, a package's submodule `from package import submodule` among them."""
    found: set[str] = set()
    for it in ast.walk(source):
        if isinstance(it, ast.ImportFrom) and it.module and not it.level:
            found.add(it.module)
            found.update(f"{it.module}.{name.name}" for name in it.names if f"{it.module}.{name.name}" in modules)
        elif isinstance(it, ast.Import):
            found.update(name.name for name in it.names)
    return found & modules


def written_again(sources: dict[str, ast.Module]) -> set[str]:
    """The modules with async code, and those that import them, however indirectly."""
    names = set(sources) | set(TWINS)
    marked = {name for name, source in sources.items() if is_async(source)} | set(TWINS)
    imports = {name: imported(source, names) for name, source in sources.items()}
    while True:
        more = {name for name, uses in imports.items() if name not in marked and uses & marked}
        if not more:
            return marked - set(TWINS)
        marked |= more


def without_async_only(text: str, source: ast.Module, module: str = "") -> str:
    """The text without the tests marked `async_only`, or the module's definitions that ASYNC_ONLY names."""
    lines = text.splitlines(keepends=True)
    dropped: set[int] = set()
    named = ASYNC_ONLY.get(module, ())
    found: set[str] = set()
    definitions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for i, it in enumerate(body):
            if isinstance(it, ast.Assign) and len(it.targets) == 1 and isinstance(it.targets[0], ast.Name):
                name = f"{prefix}{it.targets[0].id}"
                if name in named:
                    found.add(name)
                    # With the docstring that follows it, if it has one.
                    after = body[i + 1] if i + 1 < len(body) else None
                    docstring = isinstance(after, ast.Expr) and isinstance(after.value, ast.Constant)
                    last = after if docstring and after else it
                    dropped.update(range(it.lineno - 1, last.end_lineno or last.lineno))
                continue
            if not isinstance(it, definitions):
                continue
            name = f"{prefix}{it.name}"
            if name in named:
                found.add(name)
            if name in named or any(
                ast.unparse(decorator).endswith("mark.async_only") for decorator in it.decorator_list
            ):
                first = min([it.lineno, *(decorator.lineno for decorator in it.decorator_list)])
                dropped.update(range(first - 1, (it.end_lineno or it.lineno)))
            elif isinstance(it, ast.ClassDef):
                visit(it.body, f"{name}.")

    visit(source.body, "")
    missing = [it for it in named if it not in found]
    if missing:
        # A renamed definition would otherwise be written into the sync code without a word.
        raise SystemExit(f"ASYNC_ONLY names {', '.join(missing)} in {module}, which has no such definition.")
    return "".join(line for i, line in enumerate(lines) if i not in dropped)


def rewritten(text: str, generated: set[str]) -> str:
    """The text with its async code made sync, by edits to its tokens."""
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def offset(position: tuple[int, int]) -> int:
        return starts[position[0] - 1] + position[1]

    significant = [
        it for it in tokens if it.type not in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)
    ]
    edits: list[tuple[int, int, str]] = []
    i = 0
    while i < len(significant):
        token = significant[i]
        following = significant[i + 1] if i + 1 < len(significant) else None
        if (
            token.type == tokenize.NAME
            and token.string == "async"
            and following
            and following.string
            in (
                "def",
                "with",
                "for",
            )
        ):
            edits.append((offset(token.start), offset(following.start), ""))
        elif token.type == tokenize.NAME and token.string == "await" and following:
            edits.append((offset(token.start), offset(following.start), ""))
        elif token.type == tokenize.NAME and token.string in UNWRAPPED and following and following.string == "[":
            depth, j = 0, i + 1
            while True:
                depth += {"[": 1, "]": -1}.get(significant[j].string, 0)
                if depth == 0:
                    break
                j += 1
            edits.append((offset(token.start), offset(following.end), ""))
            edits.append((offset(significant[j].start), offset(significant[j].end), ""))
        elif token.type == tokenize.NAME and token.string in RENAMES:
            edits.append((offset(token.start), offset(token.end), RENAMES[token.string]))
        elif token.type == tokenize.NAME and token.string == "from" and _starts_statement(significant, i):
            j = i + 1
            while significant[j].string != "import":
                j += 1
            module = "".join(it.string for it in significant[i + 1 : j])
            target = _sync_module(module, generated)
            if target != module:
                edits.append((offset(significant[i + 1].start), offset(significant[j - 1].end), target))
            i = j
        elif token.type == tokenize.STRING:
            changed = token.string
            for old, new in STRINGS.items():
                changed = changed.replace(old, new)
            if changed != token.string:
                edits.append((offset(token.start), offset(token.end), changed))
        i += 1
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _starts_statement(tokens: list[tokenize.TokenInfo], i: int) -> bool:
    return i == 0 or tokens[i - 1].type in (tokenize.NEWLINE, tokenize.ENCODING) or tokens[i - 1].string == ":"


def _sync_module(module: str, generated: set[str]) -> str:
    if module in TWINS:
        return TWINS[module]
    if module in EXTERNAL:
        return EXTERNAL[module]
    return sync_name(module) if module in generated else module


def leftovers(text: str) -> list[str]:
    """What is still async once made sync: async syntax, or an async name in use (an unused import is removed after)."""
    found: set[str] = set()
    for it in ast.walk(ast.parse(text)):
        if isinstance(it, (ast.AsyncFunctionDef, ast.Await, ast.AsyncWith, ast.AsyncFor)):
            found.add(f"{type(it).__name__} (line {it.lineno})")
        elif isinstance(it, ast.Name) and it.id in LEFTOVERS:
            found.add(f"{it.id} (line {it.lineno})")
        elif isinstance(it, ast.Attribute) and it.attr in LEFTOVERS:
            found.add(f"{it.attr} (line {it.lineno})")
    return sorted(found)


def write(into: Path) -> list[Path]:
    """Writes the sync code under `into`, a root laid out as the repository is. Returns what it wrote."""
    paths = {
        module_of(path): path
        for package in TREES
        for path in sorted((ROOT / package.source.replace(".", "/")).rglob("*.py"))
        if tree_of(module_of(path)) is not None
    }
    sources = {name: ast.parse(path.read_text()) for name, path in paths.items()}
    generated = written_again(sources)
    written: list[Path] = []
    for name in sorted(generated):
        path = paths[name]
        text = rewritten(without_async_only(path.read_text(), sources[name], name), generated)
        left = leftovers(text)
        if left:
            raise SystemExit(f"{path.relative_to(ROOT)} still holds {', '.join(left)} once made sync.")
        source = path.relative_to(ROOT)
        header = f"# Written by scripts/unasync.py from {source}:\n# change that, and run the script again.\n"
        target = into / sync_name(name).replace(".", "/")
        target = target / "__init__.py" if path.name == "__init__.py" else target.with_suffix(".py")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(header + text)
        written.append(target)
    for package in {it.parent for it in written}:
        for parent in [package, *package.parents]:
            if parent == into or not str(parent).startswith(str(into)):
                break
            (parent / "__init__.py").touch()
    for twin in TWINS.values():
        source = ROOT / f"{twin.replace('.', '/')}.py"
        target = into / source.relative_to(ROOT)
        if source.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, target)
    dirs = [str(into / it.target.replace(".", "/")) for it in TREES]
    ruff = [sys.executable, "-m", "ruff"]
    subprocess.run([*ruff, "check", "--fix", "--quiet", "--select", "F401,I001", *dirs], check=False, cwd=ROOT)
    subprocess.run([*ruff, "format", "--quiet", *dirs], check=True, cwd=ROOT)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="write nothing; fail when the sync code is out of date")
    args = parser.parse_args()
    if not args.check:
        for tree in TREES:
            target = ROOT / tree.target.replace(".", "/")
            for path in target.rglob("*.py"):
                if module_of(path) not in TWINS.values():
                    path.unlink()
        written = write(ROOT)
        print(f"Wrote {len(written)} modules.")
        return 0
    with tempfile.TemporaryDirectory(dir=ROOT) as scratch:
        write(Path(scratch))
        stale: list[str] = []
        for tree in TREES:
            ours = ROOT / tree.target.replace(".", "/")
            fresh = Path(scratch) / tree.target.replace(".", "/")
            stale += _differences(filecmp.dircmp(ours, fresh, ignore=["__pycache__"]), ours)
        if stale:
            print(f"Out of date: {', '.join(stale)}. Run scripts/unasync.py.", file=sys.stderr)
            return 1
    return 0


def _differences(compared: filecmp.dircmp[str], base: Path) -> list[str]:
    found = [
        str(Path(compared.left, it).relative_to(ROOT))
        for it in [*compared.left_only, *compared.right_only, *compared.diff_files]
    ]
    for sub in compared.subdirs.values():
        found += _differences(sub, base)
    return found


if __name__ == "__main__":
    raise SystemExit(main())
