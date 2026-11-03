"""Set up and check the sandbox on a host.

    python -m tally.sandbox prepare [guest_root]   copy the airlock and its PDF libraries for the guest; pull the image
    python -m tally.sandbox doctor                 say whether a sandbox can run here, and if not, why
    python -m tally.sandbox open <invoice.pdf>     open one file in a fresh microVM and print its verdict
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import tally
from tally import sandbox

# The guest's Python, which must match the image (python:3.12-slim): the PDF libraries are installed for it.
GUEST_PYTHON = "3.12"
GUEST_LIBRARIES = ("pypdfium2", "pypdf", "pillow")


def copy_guest_code(root: Path) -> None:
    """Only the airlock goes into the guest: no model client, no settings, nothing that reads a key."""
    src = Path(tally.__file__).parent
    dest = root / "tally"
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "airlock").mkdir(parents=True)
    shutil.copy2(src / "__init__.py", dest / "__init__.py")
    for f in sorted((src / "airlock").glob("*.py")):
        shutil.copy2(f, dest / "airlock" / f.name)


def prepare(root: Path) -> None:
    copy_guest_code(root)
    site = root / "site"
    if site.exists():
        shutil.rmtree(site)
    # The same versions the host runs its tests with, as wheels for the guest's Python and Linux.
    pins = [f"{name}=={version(name)}" for name in GUEST_LIBRARIES]
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--target", str(site),
                    "--only-binary=:all:", "--implementation", "cp", "--python-version", GUEST_PYTHON,
                    "--platform", "manylinux_2_28_x86_64", "--platform", "manylinux_2_27_x86_64",
                    "--platform", "manylinux_2_17_x86_64", "--platform", "manylinux2014_x86_64", *pins], check=True)
    print(f"guest files ready at {root}: {', '.join(pins)}")
    if shutil.which("msb"):
        subprocess.run(["msb", "pull", sandbox.IMAGE], check=True)
    else:
        print("msb is not installed here, so the image was not pulled")


def main(argv: list[str]) -> int:
    if argv[:1] == ["prepare"] and len(argv) <= 2:
        prepare(Path(argv[1]) if len(argv) == 2 else sandbox.GUEST_ROOT)
        return 0
    if argv == ["doctor"]:
        why = sandbox.available()
        print(json.dumps({"ready": why is None, "why_not": why, "image": sandbox.IMAGE, "guest_root": str(sandbox.GUEST_ROOT)}))
        return 0 if why is None else 1
    if argv[:1] == ["open"] and len(argv) == 2:
        opened = sandbox.open_invoice(Path(argv[1]))
        r = opened.report
        print(json.dumps({"verdict": r["verdict"], "reasons": r["reasons"], "images": r["images"],
                          "seconds": round(opened.seconds, 2), "sandbox": r["sandbox"]}))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
