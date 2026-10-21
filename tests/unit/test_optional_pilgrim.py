"""
pi-pilgrim is an extra, drivine[pilgrim]: importing Drivine, async or sync, doesn't load it. Only DrivineAssembly does.
"""

import subprocess
import sys


def loads_pilgrim(statement: str) -> bool:
    """Whether running the statement in a fresh interpreter loads pi-pilgrim."""
    script = f"import sys\n{statement}\nprint('pipilgrim' in sys.modules)"
    return (
        subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout.strip()
        == "True"
    )


def test_imports_drivine_without_pilgrim():
    assert not loads_pilgrim("import drivine, drivine.sync")


def test_imports_pilgrim_for_the_assembly():
    assert loads_pilgrim("from drivine.drivine_assembly import DrivineAssembly")
    assert loads_pilgrim("from drivine.sync.drivine_assembly import DrivineAssembly")
