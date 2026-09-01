from __future__ import annotations

import importlib
import os
import site
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VENV = (PROJECT_ROOT / ".venv").resolve()
PACKAGES = ("numpy", "pandas", "pyarrow", "streamlit", "plotly", "pydantic")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    problems: list[str] = []
    executable = Path(sys.executable).resolve()
    print(f"sys.executable={executable}")
    print(f"expected_venv={EXPECTED_VENV}")
    if not _inside(executable, EXPECTED_VENV):
        problems.append("Python executable is outside the project .venv")

    for package in PACKAGES:
        try:
            module = importlib.import_module(package)
        except Exception as exc:
            problems.append(f"{package} import failed: {type(exc).__name__}: {exc}")
            continue
        version = getattr(module, "__version__", "unknown")
        origin = Path(getattr(module, "__file__", "")).resolve()
        print(f"{package}.version={version}")
        print(f"{package}.path={origin}")
        if not _inside(origin, EXPECTED_VENV):
            problems.append(f"{package} loaded outside the project .venv: {origin}")

    user_site = Path(site.getusersitepackages()).resolve()
    mixed_paths = []
    for entry in sys.path:
        if not entry:
            continue
        path = Path(entry).resolve()
        if "site-packages" in str(path).lower() and not _inside(path, EXPECTED_VENV):
            mixed_paths.append(str(path))
    print(f"user_site_enabled={site.ENABLE_USER_SITE}")
    print(f"user_site={user_site}")
    print("external_site_packages=" + ("none" if not mixed_paths else os.pathsep.join(mixed_paths)))
    if mixed_paths:
        problems.append("external site-packages detected on sys.path")

    if problems:
        for problem in problems:
            print(f"ERROR={problem}")
        return 1
    print("runtime_check=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
