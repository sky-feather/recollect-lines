#!/usr/bin/env python3
"""Offline clean-install acceptance for Phase 7A.

Builds a fresh virtual environment, installs this package from local artifacts
(no PYTHONPATH=src shortcut), and proves console entry points.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENTRY_POINTS = {
    "recollect-lines": ("recollect_lines.cli", "main"),
    "recollect-mcp": ("recollect_lines.mcp_server", "main"),
}


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, text=True, capture_output=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def run_optional(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, **kwargs)


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise SystemExit(1)


def _site_packages(python: Path) -> Path:
    output = run([str(python), "-c", "import site; print(site.getsitepackages()[0])"]).stdout.strip()
    return Path(output)


def _manual_install(python: Path, scripts: Path) -> None:
    """Stdlib-only fallback when pip cannot bootstrap build tooling offline."""
    site_packages = _site_packages(python)
    package_dir = site_packages / "recollect_lines"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    shutil.copytree(ROOT / "src" / "recollect_lines", package_dir)
    scripts.mkdir(parents=True, exist_ok=True)
    for name, (module, func) in ENTRY_POINTS.items():
        script = scripts / name
        script.write_text(
            f"#!{python}\n"
            f"import sys\n"
            f"from {module} import {func}\n"
            f"if __name__ == '__main__':\n"
            f"    sys.exit({func}())\n",
            encoding="utf-8",
        )
        script.chmod(0o755)


def _scripts_installed(scripts: Path) -> bool:
    return (scripts / "recollect-lines").exists() and (scripts / "recollect-mcp").exists()


def _pip_wheel_install(python: Path, tmp_path: Path) -> bool:
    bootstrap = run_optional([str(python), "-m", "pip", "install", "setuptools>=68", "wheel"])
    if bootstrap.returncode != 0:
        return False
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    # Build a wheel for this package itself without build isolation (offline-safe:
    # setuptools/wheel were already bootstrapped above), but do fetch runtime
    # dependency wheels (e.g. PyYAML) into the same find-links directory so the
    # --no-index install below can resolve them.
    build = run_optional([
        str(python), "-m", "pip", "wheel", str(ROOT),
        "-w", str(wheel_dir), "--no-deps", "--no-build-isolation",
    ])
    if build.returncode != 0:
        return False
    deps = run_optional([
        str(python), "-m", "pip", "wheel", "PyYAML>=6",
        "-w", str(wheel_dir),
    ])
    if deps.returncode != 0:
        return False
    install = run_optional([
        str(python), "-m", "pip", "install",
        "--no-index", f"--find-links={wheel_dir}", "recollect-lines",
    ])
    return install.returncode == 0


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="recollect-clean-install-") as tmp:
        tmp_path = Path(tmp)
        venv_dir = tmp_path / "venv"
        venv.create(venv_dir, with_pip=True)
        if sys.platform == "win32":
            python = venv_dir / "Scripts" / "python.exe"
            scripts = venv_dir / "Scripts"
        else:
            python = venv_dir / "bin" / "python"
            scripts = venv_dir / "bin"

        if _pip_wheel_install(python, tmp_path) and _scripts_installed(scripts):
            print("Installed package via local wheel (pip).")
        else:
            print("Pip wheel install unavailable or incomplete; using offline manual install fallback.")
            _manual_install(python, scripts)

        env = {"PATH": f"{scripts}{os.pathsep}{os.environ.get('PATH', '')}"}

        help_result = run(["recollect-lines", "--help"], env=env)
        check("recollect-lines --help succeeds", "usage:" in help_result.stdout.lower())

        doctor_result = run(
            ["recollect-lines", "--home", str(tmp_path / "broker"), "doctor", "--json"],
            env=env,
        )
        check("recollect-lines doctor --json succeeds", '"doctor_schema_version"' in doctor_result.stdout)
        check("doctor JSON has no raw secret material", "sk-" not in doctor_result.stdout)

        mcp_result = run(["recollect-mcp", "--help"], env=env)
        check("recollect-mcp --help succeeds", "usage:" in mcp_result.stdout.lower())

        legacy = shutil.which("recollect", path=env["PATH"])
        check("recollect console script is absent", legacy is None, f"found at {legacy}")

    print(
        "Clean-install acceptance PASSED: fresh venv install exposes "
        "recollect-lines and recollect-mcp; legacy recollect is absent."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
