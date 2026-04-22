"""Create a virtual environment and install project dependencies."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent
    venv_dir = repo_root / ".venv"

    print(f"Creating virtual environment at {venv_dir} ...")
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    if sys.platform == "win32":
        pip = venv_dir / "Scripts" / "pip.exe"
    else:
        pip = venv_dir / "bin" / "pip"

    print("Installing dependencies from requirements.txt ...")
    subprocess.run(
        [str(pip), "install", "-r", str(repo_root / "requirements.txt")],
        check=True,
    )

    print("Done. Virtual environment is ready at .venv/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
