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
from typing import TextIO

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
_CLAUSE_RE = re.compile(
    r"^(?P<op>>=|<=|!=|==|~=|>|<)?\s*"
    r"(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<micro>\d+))?(?P<wild>\.\*)?$"
)


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


@dataclass(frozen=True)
class Clause:
    op: str
    major: int
    minor: int | None
    micro: int | None
    wildcard: bool

    @property
    def precision(self) -> int:
        if self.micro is not None:
            return 3
        if self.minor is not None:
            return 2
        return 1

    def floor(self) -> tuple[int, int, int]:
        return (self.major, self.minor or 0, self.micro or 0)


def python_satisfies(version: Version, spec: str) -> bool:
    """True when ``version`` matches a requires-python specifier."""
    clauses = [part.strip() for part in spec.split(",") if part.strip()]
    if not clauses:
        raise ValueError("empty requires-python")
    return all(_clause_matches(version, clause) for clause in clauses)


def _parse_clause(clause: str) -> Clause:
    match = _CLAUSE_RE.match(clause.strip())
    if match is None:
        raise ValueError(f"unsupported requires-python clause: {clause}")
    minor = match.group("minor")
    micro = match.group("micro")
    return Clause(
        op=match.group("op") or "==",
        major=int(match.group("major")),
        minor=int(minor) if minor is not None else None,
        micro=int(micro) if micro is not None else None,
        wildcard=match.group("wild") is not None,
    )


def _clause_matches(version: Version, clause: str) -> bool:
    parsed = _parse_clause(clause)
    current = version.as_tuple()
    if parsed.op == "~=":
        if parsed.wildcard:
            raise ValueError(f"unsupported requires-python clause: {clause}")
        return _compatible_release(current, parsed)
    if parsed.wildcard:
        if parsed.op not in {"==", "!="}:
            raise ValueError(f"unsupported requires-python clause: {clause}")
        matched = _wildcard_match(current, parsed)
        return matched if parsed.op == "==" else not matched
    return _compare(current, parsed.op, parsed.floor())


def _wildcard_match(current: tuple[int, int, int], parsed: Clause) -> bool:
    if parsed.minor is None:
        return current[0] == parsed.major
    if parsed.micro is None:
        return current[0] == parsed.major and current[1] == parsed.minor
    return current == (parsed.major, parsed.minor, parsed.micro)


def _compatible_release(current: tuple[int, int, int], parsed: Clause) -> bool:
    """PEP 440 ``~=`` : ``~=3.11`` is ``>=3.11, ==3.*`` and ``~=3.11.2`` is ``>=3.11.2, ==3.11.*``."""
    if current < parsed.floor():
        return False
    if parsed.precision == 1:
        return current[0] == parsed.major
    if parsed.precision == 2:
        return current[0] == parsed.major
    return current[0] == parsed.major and current[1] == parsed.minor


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


def iter_lock_pins(lock_text: str) -> list[tuple[str, str, str]]:
    """Exact pins as ``(name, version, marker)``. ``marker`` is empty when absent."""
    entries: list[tuple[str, str, str]] = []
    for line in lock_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN_RE.match(stripped)
        if match is None:
            continue
        marker = stripped.split(";", 1)[1].strip() if ";" in stripped else ""
        entries.append((normalize_name(match.group(1)), match.group(2), marker))
    return entries


def lock_pins(lock_text: str) -> dict[str, str]:
    """Map normalized project names to exact versions.

    When a name is pinned more than once, the last line wins. Use
    ``iter_lock_pins`` to see every marker-specific pin.
    """
    return {name: version for name, version, _marker in iter_lock_pins(lock_text)}


def pip_requirement(lock_text: str) -> str:
    """The ``pip==X`` pin setup installs instead of upgrading to the newest pip."""
    version = lock_pins(lock_text).get("pip")
    if not version:
        raise ValueError(
            "backend/requirements.lock does not pin pip. Regenerate it so pip==... is included."
        )
    return f"pip=={version}"


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
        ("installing pip", [*pip, pip_requirement(lock.read_text(encoding="utf-8"))]),
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
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return CommandResult(127, "", f"command not found: {command[0]}")
    if not capture:
        print(note("running " + format_command(command)), flush=True)
        _write_captured(sys.stdout, completed.stdout)
        _write_captured(sys.stderr, completed.stderr)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _write_captured(stream: TextIO, text: str) -> None:
    if not text:
        return
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    stream.flush()


def probe_python_version(python: str, run: Runner) -> Version:
    result = run([python, "-c", PYTHON_VERSION_SNIPPET], cwd=None, capture=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"exit {result.returncode}")
    return parse_python_version(result.stdout.strip())


def describe_remove_failure(exc: OSError) -> str:
    """Explain a refused or failed ``backend/.venv`` removal without ``[Errno None]``."""
    detail = " ".join(part for part in (exc.strerror, str(exc)) if part).lower()
    if "symlink" in detail or "symbolic link" in detail:
        return (
            "backend/.venv is a symlink. Setup left it in place. "
            "Remove the symlink manually and rerun."
        )
    if exc.errno is None:
        return f"could not remove backend/.venv: {exc.strerror or exc}"
    return f"could not remove backend/.venv: {exc}"


def remove_path(path: Path) -> None:
    if path.is_symlink():
        raise OSError(
            "backend/.venv is a symlink. Setup left it in place. "
            "Remove the symlink manually and rerun."
        )
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def environment_is_broken(result: CommandResult) -> bool:
    """True when pip or site-packages failed, rather than a dependency resolution."""
    if result.returncode == 0:
        return False
    if result.returncode == 127:
        return True
    text = f"{result.stdout}\n{result.stderr}".lower()
    markers = (
        "no module named pip",
        "no module named 'pip'",
        'no module named "pip"',
        "bad interpreter",
        "site-packages",
        "modulenotfounderror",
        "importerror",
    )
    return any(marker in text for marker in markers)


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


def _recreate_venv(
    action: str,
    venv_dir: Path,
    python: str,
    repo_root: Path,
    run: Runner,
    log: Log,
    remove: Callable[[Path], None],
) -> int:
    """Remove and create the virtualenv when needed. Return 0 on success."""
    if action == "reuse":
        return 0
    if action == "recreate":
        try:
            remove(venv_dir)
        except OSError as exc:
            log(fail(describe_remove_failure(exc)))
            return 1
        log(ok("removed backend/.venv"))
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
    return 0


def _install_dependencies(
    steps: list[tuple[str, list[str]]],
    *,
    backend: Path,
    venv_dir: Path,
    python: str,
    repo_root: Path,
    run: Runner,
    log: Log,
    remove: Callable[[Path], None],
    can_repair: bool,
) -> int:
    """Install pinned dependencies. Recreate once when a reused venv's pip is broken."""
    repaired = False
    while True:
        failure: tuple[str, CommandResult] | None = None
        for label, args in steps:
            log(note(label))
            blocked = command_blocked_host(args)
            if blocked is not None:
                log(fail(f"refusing {label}: command would contact {blocked}"))
                return 1
            result = run(args, cwd=backend, capture=False)
            if result.returncode != 0:
                failure = (label, result)
                break
            log(ok(label))
        if failure is None:
            return 0
        label, result = failure
        if can_repair and not repaired and environment_is_broken(result):
            log(
                note(
                    "pip or site-packages in backend/.venv failed. "
                    "Recreating the virtualenv and retrying once."
                )
            )
            if _recreate_venv("recreate", venv_dir, python, repo_root, run, log, remove) != 0:
                return 1
            repaired = True
            continue
        log(fail(f"{label} failed (exit {result.returncode})."))
        detail = (result.stderr or result.stdout).strip()
        if detail:
            log(detail)
        if environment_is_broken(result):
            log(
                fail(
                    "pip or site-packages in backend/.venv is broken. "
                    "Run bash scripts/setup.sh --force to recreate it."
                )
            )
        return 1


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

    try:
        matches = python_satisfies(version, spec)
    except ValueError as exc:
        log(fail(f"cannot interpret requires-python {spec!r}: {exc}"))
        return 1
    if not matches:
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
            venv_matches = python_satisfies(venv_version, spec)
        except (OSError, ValueError, RuntimeError) as exc:
            log(note(f"existing backend/.venv did not run ({exc}); recreating"))
        else:
            same_python = venv_version.as_tuple() == version.as_tuple()
            healthy = venv_matches and same_python
            if exists and not venv_matches:
                log(
                    note(
                        f"existing backend/.venv has Python {venv_version}, "
                        f"outside {spec}; recreating"
                    )
                )
            elif exists and not same_python:
                log(
                    note(
                        f"existing backend/.venv has Python {venv_version}; "
                        f"{python} is Python {version}; recreating"
                    )
                )
    elif exists:
        log(note("existing backend/.venv is incomplete; recreating"))

    action = decide_venv_action(exists=exists, force=options.force, healthy=healthy)
    if _recreate_venv(action, venv_dir, python, repo_root, run, log, remove) != 0:
        return 1
    if action == "reuse":
        log(ok("reusing backend/.venv"))

    if not lock.is_file():
        log(fail("backend/requirements.lock is missing. Setup will not install unpinned packages."))
        return 1

    venv_py = str(interpreter)
    try:
        steps = install_steps(venv_py, lock)
    except ValueError as exc:
        log(fail(str(exc)))
        return 1
    if (
        _install_dependencies(
            steps,
            backend=backend,
            venv_dir=venv_dir,
            python=python,
            repo_root=repo_root,
            run=run,
            log=log,
            remove=remove,
            can_repair=action == "reuse",
        )
        != 0
    ):
        return 1

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
