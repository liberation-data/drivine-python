"""
Installs a built wheel into a clean virtual environment, outside the repository, and checks that it works: drivine
imports, and `python -m drivine.stubs` writes the typed references of a small project's models. The release workflow
runs it on the wheel it publishes:

    python scripts/smoke_wheel.py dist/drivine-X.Y.Z-py3-none-any.whl

It reads the standard library alone, so nothing is installed for it but the wheel, into the environment it makes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

MODELS = """\
from typing import Annotated

from drivine import GraphView, NodeFragment, NodeId, Relationship, RelationshipFragment, Root, TargetNode


class Person(NodeFragment, labels=["Person"]):
    uuid: Annotated[str, NodeId()]
    name: str


class Organization(NodeFragment, labels=["Organization"]):
    uuid: Annotated[str, NodeId()]
    name: str


class WorkHistory(RelationshipFragment):
    role: str
    organization: Annotated[Organization, TargetNode()]


class PersonCareer(GraphView):
    person: Annotated[Person, Root()]
    employment_history: Annotated[list[WorkHistory], Relationship("WORKS_FOR")] = []
"""

# drivine is imported from the environment's site-packages, from the wheel, and not from a checkout.
IMPORTS = (
    "import sys, drivine; assert drivine.__file__.startswith(sys.prefix), drivine.__file__; print(drivine.__file__)"
)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].endswith(".whl"):
        print("Usage: python scripts/smoke_wheel.py <the wheel>", file=sys.stderr)
        return 2
    wheel = Path(argv[0]).resolve()
    with tempfile.TemporaryDirectory(prefix="drivine-smoke-") as root:
        home = Path(root)
        venv.create(home / "venv", with_pip=True)
        python = home / "venv" / ("Scripts" if sys.platform == "win32" else "bin") / "python"
        project = home / "project"
        (project / "smoke").mkdir(parents=True)
        (project / "pyproject.toml").write_text('[project]\nname = "smoke"\nversion = "0"\n')
        (project / "smoke" / "__init__.py").write_text("")
        (project / "smoke" / "models.py").write_text(MODELS)

        # Nothing from the repository or the caller's environment is on the path: drivine comes from the wheel alone.
        env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "VIRTUAL_ENV")}
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

        def run(*args: str) -> None:
            print("+", " ".join(args), flush=True)
            subprocess.run(args, cwd=project, env=env, check=True)

        run(str(python), "-m", "pip", "install", "--quiet", str(wheel))
        run(str(python), "-c", IMPORTS)
        run(str(python), "-m", "drivine.stubs", "smoke.models")
        written = [
            project / "smoke" / "models_refs.py",
            project / "smoke" / "models_refs.pyi",
            project / ".drivine" / "model.json",
        ]
        missing = [str(path.relative_to(project)) for path in written if not path.is_file()]
        if missing:
            print(f"python -m drivine.stubs didn't write {', '.join(missing)}.", file=sys.stderr)
            return 1
        run(str(python), "-m", "drivine.stubs", "smoke.models", "--check")
        run(
            str(python),
            "-c",
            "from smoke.models_refs import refs; from smoke.models import PersonCareer; "
            "print(refs(PersonCareer).person.name.eq('Ada'))",
        )
    print(f"{wheel.name}: installs, imports, and writes typed references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
