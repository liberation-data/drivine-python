"""
Writes typed references for the models of some modules:

    python -m drivine.stubs myapp.models                  # writes myapp/models_refs.py and models_refs.pyi
    python -m drivine.stubs myapp.models myapp.billing --out myapp/query_refs.py
    python -m drivine.stubs myapp.models --check          # fails when they are out of date, for CI

Then import `refs` from the module written, rather than from drivine, and a type checker checks each query's fields.

It writes the model file too, `.drivine/model.json` beside the project's pyproject.toml, which Drivine Studio reads:
the models' fragments, relationship fragments and views, and how each is stored. `--no-model`, or `model = false`
under `[tool.drivine]` in pyproject.toml, writes none.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from drivine.drivine_error import DrivineError
from drivine.stubs.model_file import Project, write_model_file
from drivine.stubs.stub_writer import StubWriter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m drivine.stubs", description=(__doc__ or "").split("\n\n")[0].strip()
    )
    parser.add_argument("modules", nargs="+", help="the modules whose models to type, by dotted name")
    parser.add_argument("--out", help="the module to write; by default beside the first, with _refs added")
    parser.add_argument("--check", action="store_true", help="write nothing; fail when the files are out of date")
    parser.add_argument("--no-model", action="store_true", help="write no model file, .drivine/model.json")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path.cwd()))
    out = Path(args.out) if args.out else _beside(args.modules[0])
    command = " ".join(["python -m drivine.stubs", *args.modules, *(["--out", args.out] if args.out else [])])
    runtime, stub = StubWriter(args.modules, command).write()
    files = {out.with_suffix(".py"): runtime, out.with_suffix(".pyi"): stub}
    stale_model = None if args.no_model else _model_file(args.modules, out, args.check)
    if args.check:
        stale = [str(path) for path, text in files.items() if not path.exists() or path.read_text() != text]
        if stale_model:
            stale.append(str(stale_model))
        if stale:
            print(f"Out of date: {', '.join(stale)}. Run {command} to write them again.", file=sys.stderr)
            return 1
        return 0
    for path, text in files.items():
        path.write_text(text)
        print(f"Wrote {path}")
    return 0


def _model_file(modules: list[str], out: Path, check: bool) -> Path | None:
    """
    Writes the model file of the project the written module is in, or with `check` gives its path when it is out of
    date, to be named with the stubs. A module in no project, with no pyproject.toml above it, gets none: there is
    nowhere the file belongs.
    """
    try:
        project = Project.of(out.resolve().parent)
    except DrivineError as error:
        print(f"Drivine model file: not written. {error}", file=sys.stderr)
        return None
    outcome = write_model_file(modules, project, check, lambda it: print(it, file=sys.stderr))
    if outcome == "written":
        print(f"Wrote {project.file}")
    return project.file if outcome == "stale" else None


def _beside(module: str) -> Path:
    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        raise SystemExit(f"Can't find the module {module}.")
    origin = Path(spec.origin)
    return origin.with_name(f"{origin.stem}_refs.py")


if __name__ == "__main__":
    raise SystemExit(main())
