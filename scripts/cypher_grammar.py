"""
Writes the Cypher compiler's parser, drivine/cypher/grammar/CypherLexer.py and CypherParser.py, from openCypher's
grammar as drivine4j extends it, drivine/cypher/grammar/Cypher.g4, with ANTLR's Python 3 target. Cypher.g4 is
drivine4j's src/main/antlr/org/drivine/cypher/grammar/Cypher.g4, byte for byte: change it there first, copy it here,
and run this again.

    uv run python scripts/cypher_grammar.py

The tool is a Java program: this needs `java` on the path, and downloads the tool's jar once, of the version the
runtime (antlr4-python3-runtime) is, as drivine4j's build uses it.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAMMAR = ROOT / "drivine" / "cypher" / "grammar"
VERSION = "4.13.2"
JAR = f"https://repo1.maven.org/maven2/org/antlr/antlr4/{VERSION}/antlr4-{VERSION}-complete.jar"
SHA256 = "eae2dfa119a64327444672aff63e9ec35a20180dc5b8090b7a6ab85125df4d76"
GENERATED = ("CypherLexer.py", "CypherParser.py")


def tool() -> Path:
    """The tool's jar, downloaded once into the user's cache, and checked against the digest it was released with."""
    jar = Path.home() / ".cache" / "drivine" / f"antlr-{VERSION}-complete.jar"
    if not jar.exists():
        jar.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(JAR) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != SHA256:
            raise SystemExit(f"{JAR} is not the jar ANTLR {VERSION} was released as.")
        jar.write_bytes(content)
    return jar


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.parse_args()
    if shutil.which("java") is None:
        raise SystemExit("ANTLR's tool is a Java program: put java on the path.")
    jar = tool()
    with tempfile.TemporaryDirectory() as out:
        # Run from the grammar's folder, so the generated files name it as Cypher.g4, and not by a path of this machine.
        subprocess.run(
            ["java", "-jar", str(jar), "-Dlanguage=Python3", "-no-listener", "-no-visitor", "-o", out, "Cypher.g4"],
            cwd=GRAMMAR,
            check=True,
        )
        for name in GENERATED:
            shutil.copyfile(Path(out) / name, GRAMMAR / name)
    print(f"{', '.join(GENERATED)} are written from Cypher.g4 by ANTLR {VERSION}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
