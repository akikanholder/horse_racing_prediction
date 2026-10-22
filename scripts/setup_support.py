#!/usr/bin/env python3
"""Create backend/.venv from the pinned lock and run an offline self-check.

The shell entry points are scripts/setup.sh and scripts/setup.ps1. This module
uses only the standard library so it can run before the virtualenv exists.

The self-check imports the package, prints CLI help, runs a small
``pytest -m "not integration"`` subset, and launches headless Chromium on
``about:blank``. It does not request JRA, netkeiba, or the other racing sites.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
VENV_DIR = BACKEND_DIR / ".venv"
LOCK_FILE = BACKEND_DIR / "requirements.lock"
PYPROJECT_FILE = BACKEND_DIR / "pyproject.toml"

PYTHON_VERSION_SNIPPET = "import sys; print('{}.{}.{}'.format(*sys.version_info[:3]))"
IMPORT_SNIPPET = "import backend, backend.cli\nprint('imported backend')"
CHROMIUM_OFFLINE_SNIPPET = """\
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.goto("about:blank")
        url = page.url
    finally:
        browser.close()
if url != "about:blank":
    raise SystemExit("headless Chromium opened " + url)
print("headless Chromium launched (about:blank)")
"""
SMOKE_PYTEST_ARGS = [
    "-m",
    "not integration",
    "tests/test_csv_export.py",
    "-q",
    "--tb=short",
]

# Self-check and install commands must not contact these hosts.
BLOCKED_HOSTS = (
    "jra.go.jp",
    "jra.jp",
    "netkeiba.com",
    "keibabook.co.jp",
    "speedkeiba.com",
)

_VERSION_RE = re.compile(r"(?:Python\s+)?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_REQUIRES_RE = re.compile(r"""(?m)^requires-python\s*=\s*["']([^"']+)["']\s*$""")
_REQ_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)")
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;\\]+)\s*(?:;.*)?$")
_OPS = (">=", "<=", "!=", "==", ">", "<")


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    micro: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.micro)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.micro}"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Options:
    force: bool
    python: str | None


Runner = Callable[..., CommandResult]
Log = Callable[[str], None]


def ok(message: str) -> str:
    return f"[setup] OK   {message}"


def fail(message: str) -> str:
    return f"[setup] FAIL {message}"


def note(message: str) -> str:
    return f"[setup] note {message}"


def parse_python_version(text: str) -> Version:
    """Parse ``4``, ``3.11``, ``3.11.4``, or ``Python 3.11.4``."""
    match = _VERSION_RE.search(text.strip())
    if match is None:
        raise ValueError(f"cannot parse a Python version from {text!r}")
    minor = int(match.group(2) or 0)
    micro = int(match.group(3) or 0)
    return Version(int(match.group(1)), minor, micro)


def read_requires_python(pyproject_text: str) -> str:
    match = _REQUIRES_RE.search(pyproject_text)
    if match is None:
        raise ValueError("requires-python is not set in pyproject.toml")
    return match.group(1).strip()


def python_satisfies(version: Version, spec: str) -> bool:
    """True when ``version`` matches a requires-python specifier of comparison clauses."""
    clauses = [part.strip() for part in spec.split(",") if part.strip()]
    if not clauses:
        raise ValueError("empty requires-python")
    return all(_clause_matches(version, clause) for clause in clauses)


def _clause_matches(version: Version, clause: str) -> bool:
    for op in _OPS:
        if clause.startswith(op):
            other = parse_python_version(clause[len(op) :].strip())
            return _compare(version.as_tuple(), op, other.as_tuple())
    raise ValueError(f"unsupported requires-python clause: {clause}")


def _compare(left: tuple[int, int, int], op: str, right: tuple[int, int, int]) -> bool:
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    raise ValueError(f"unsupported operator: {op}")


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(spec: str) -> str:
    body = spec.split("#", 1)[0].strip()
    body = body.split("[", 1)[0].strip()
    match = _REQ_NAME_RE.match(body)
    if match is None:
        raise ValueError(f"cannot parse a requirement name from {spec!r}")
    return normalize_name(match.group(1))


def lock_pins(lock_text: str) -> dict[str, str]:
    """Map normalized project names to exact versions in a pip-compile lock."""
    pins: dict[str, str] = {}
    for line in lock_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN_RE.match(stripped)
        if match is None:
            continue
        pins[normalize_name(match.group(1))] = match.group(2)
    return pins


def venv_python(venv_dir: Path, platform: str | None = None) -> Path:
    system = platform if platform is not None else sys.platform
    if system == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def decide_venv_action(*, exists: bool, force: bool, healthy: bool) -> str:
    """Return ``create``, ``reuse``, or ``recreate``."""
    if not exists:
        return "create"
    if force or not healthy:
        return "recreate"
    return "reuse"


def command_blocked_host(args: Sequence[str]) -> str | None:
    blob = " ".join(args).lower()
    for host in BLOCKED_HOSTS:
        if host in blob:
            return host
    return None


def venv_create_command(python: str, dest: Path) -> list[str]:
    return [python, "-m", "venv", str(dest)]


def install_steps(python: str, lock: Path) -> list[tuple[str, list[str]]]:
    """Pip and Playwright commands. The editable install runs with cwd=backend."""
    pip = [python, "-m", "pip", "install", "--disable-pip-version-check"]
    return [
        ("upgrading pip", [*pip, "--upgrade", "pip"]),
        (
            "installing pinned dependencies (including headless)",
            [
                *pip,
                "--upgrade",
                "--constraint",
                str(lock.resolve()),
                "--editable",
                ".[headless,dev]",
            ],
        ),
        (
            "installing Playwright Chromium",
            [python, "-m", "playwright", "install", "chromium"],
        ),
    ]


def self_check_steps(python: str) -> list[tuple[str, list[str]]]:
    return [
        ("import backend", [python, "-c", IMPORT_SNIPPET]),
        ("python -m backend.cli --help", [python, "-m", "backend.cli", "--help"]),
        (
            'pytest -m "not integration" tests/test_csv_export.py',
            [python, "-m", "pytest", *SMOKE_PYTEST_ARGS],
        ),
        (
            "headless Chromium launches (about:blank)",
            [python, "-c", CHROMIUM_OFFLINE_SNIPPET],
        ),
    ]


def activate_hint(platform: str | None = None) -> str:
    system = platform if platform is not None else sys.platform
    if system == "win32":
        return r"backend\.venv\Scripts\Activate.ps1"
    return "source backend/.venv/bin/activate"


def venv_failure_hint(stderr: str) -> str:
    if "ensurepip" in stderr.lower():
        return (
            "On Debian/Ubuntu, install the venv package for this Python "
            "(for example: sudo apt install python3.12-venv) and rerun."
        )
    return ""


def describe_node(which: Callable[[str], str | None], run: Runner) -> str:
    path = which("node")
    if not path:
        return note("node not found (optional, not required)")
    result = run([path, "--version"], cwd=None, capture=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        return note(f"node is on PATH but --version failed ({detail}). Optional, setup continues.")
    return note(f"node {result.stdout.strip()} (optional, not required)")


def parse_options(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(
        prog="scripts/setup.sh",
        description=(
            "Create backend/.venv, install the locked dependencies including "
            "Playwright, and run an offline self-check."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete backend/.venv and create it again",
    )
    parser.add_argument(
        "--python",
        help="interpreter used to create the virtualenv (default: this Python)",
    )
    args = parser.parse_args(None if argv is None else list(argv))
    return Options(force=args.force, python=args.python)


def format_command(args: Sequence[str]) -> str:
    """One-line display. Inline ``-c`` scripts stay out of the log."""
    parts: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-c" and index + 1 < len(args):
            parts.append("-c")
            parts.append("<script>")
            index += 2
            continue
        parts.append(arg)
        index += 1
    return " ".join(parts)


def default_runner(
    args: Sequence[str],
    *,
    cwd: Path | None,
    capture: bool,
) -> CommandResult:
    command = list(args)
    try:
        if capture:
            completed = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                text=True,
                capture_output=True,
            )
            return CommandResult(completed.returncode, completed.stdout, completed.stderr)
        print(note("running " + format_command(command)), flush=True)
        completed = subprocess.run(command, cwd=cwd, check=False)
        return CommandResult(completed.returncode, "", "")
    except FileNotFoundError:
        return CommandResult(127, "", f"command not found: {command[0]}")


def probe_python_version(python: str, run: Runner) -> Version:
    result = run([python, "-c", PYTHON_VERSION_SNIPPET], cwd=None, capture=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"exit {result.returncode}")
    return parse_python_version(result.stdout.strip())


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _run_checked(
    label: str,
    args: list[str],
    *,
    cwd: Path | None,
    run: Runner,
    log: Log,
    capture: bool,
) -> int | None:
    blocked = command_blocked_host(args)
    if blocked is not None:
        log(fail(f"refusing {label}: command would contact {blocked}"))
        return 1
    result = run(args, cwd=cwd, capture=capture)
    if result.returncode != 0:
        log(fail(f"{label} failed (exit {result.returncode})"))
        if capture and result.stderr.strip():
            log(result.stderr.strip())
        return 1
    return None


def run_setup(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path = REPO_ROOT,
    run: Runner = default_runner,
    log: Log = print,
    platform: str | None = None,
    remove: Callable[[Path], None] = remove_path,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    """Run the full setup. Return a process exit code."""
    options = parse_options([] if argv is None else argv)
    system = platform if platform is not None else sys.platform
    backend = repo_root / "backend"
    venv_dir = backend / ".venv"
    lock = backend / "requirements.lock"
    pyproject = backend / "pyproject.toml"
    python = options.python or sys.executable

    try:
        spec = read_requires_python(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log(fail(f"cannot read requires-python from backend/pyproject.toml: {exc}"))
        return 1

    try:
        version = probe_python_version(python, run)
    except (OSError, ValueError, RuntimeError) as exc:
        log(fail(f"could not run {python}: {exc}"))
        return 1

    if not python_satisfies(version, spec):
        log(
            fail(
                f"Python {version} ({python}) is outside requires-python {spec}. "
                "Install a matching Python and rerun."
            )
        )
        return 1
    log(ok(f"Python {version} satisfies {spec} ({python})"))

    interpreter = venv_python(venv_dir, system)
    exists = venv_dir.exists()
    healthy = False
    if interpreter.is_file():
        try:
            venv_version = probe_python_version(str(interpreter), run)
        except (OSError, ValueError, RuntimeError) as exc:
            log(note(f"existing backend/.venv did not run ({exc}); recreating"))
        else:
            healthy = python_satisfies(venv_version, spec)
            if exists and not healthy:
                log(
                    note(
                        f"existing backend/.venv has Python {venv_version}, "
                        f"outside {spec}; recreating"
                    )
                )
    elif exists:
        log(note("existing backend/.venv is incomplete; recreating"))

    action = decide_venv_action(exists=exists, force=options.force, healthy=healthy)
    if action == "recreate":
        try:
            remove(venv_dir)
        except OSError as exc:
            log(fail(f"could not remove backend/.venv: {exc}"))
            return 1
        log(ok("removed backend/.venv"))

    if action in {"create", "recreate"}:
        creating = "creating backend/.venv" if action == "create" else "recreating backend/.venv"
        log(note(creating))
        created = run(venv_create_command(python, venv_dir), cwd=repo_root, capture=True)
        if created.returncode != 0:
            hint = venv_failure_hint(created.stderr)
            log(fail(f"could not create backend/.venv (exit {created.returncode}). {hint}".strip()))
            if created.stderr.strip():
                log(created.stderr.strip())
            return 1
        log(ok(creating))
    else:
        log(ok("reusing backend/.venv"))

    if not lock.is_file():
        log(fail("backend/requirements.lock is missing. Setup will not install unpinned packages."))
        return 1

    venv_py = str(interpreter)
    for label, args in install_steps(venv_py, lock):
        log(note(label))
        status = _run_checked(label, args, cwd=backend, run=run, log=log, capture=False)
        if status is not None:
            return status
        log(ok(label))

    log(describe_node(which, run))

    for label, args in self_check_steps(venv_py):
        log(note(f"self-check: {label}"))
        status = _run_checked(
            f"self-check: {label}",
            args,
            cwd=backend,
            run=run,
            log=log,
            capture=False,
        )
        if status is not None:
            return status
        log(ok(f"self-check: {label}"))

    log(ok("setup complete"))
    log(note(f"activate with: {activate_hint(system)}"))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run_setup(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
