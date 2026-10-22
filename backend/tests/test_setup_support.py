"""Unit tests for the stdlib setup helper in scripts/setup_support.py."""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

_SUPPORT = Path(__file__).resolve().parents[2] / "scripts" / "setup_support.py"
_SPEC = importlib.util.spec_from_file_location("setup_support", _SUPPORT)
assert _SPEC is not None and _SPEC.loader is not None
setup_support = importlib.util.module_from_spec(_SPEC)
sys.modules["setup_support"] = setup_support
_SPEC.loader.exec_module(setup_support)


class FakeRun:
    def __init__(self, versions: dict[str, str] | None = None, fail_substring: str | None = None):
        self.calls: list[list[str]] = []
        self.cwds: list[Path | None] = []
        self.versions = versions or {}
        self.fail_substring = fail_substring
        self.stderr = "failed"

    def __call__(self, args, *, cwd, capture):
        command = list(args)
        self.calls.append(command)
        self.cwds.append(cwd)
        text = " ".join(command)
        if self.fail_substring and self.fail_substring in text:
            return setup_support.CommandResult(1, "", self.stderr)
        if len(command) >= 3 and command[1] == "-c" and "version_info" in command[2]:
            version = self.versions.get(command[0], "3.12.3")
            return setup_support.CommandResult(0, version + "\n", "")
        if command[-1] == "--version":
            return setup_support.CommandResult(0, "v22.14.0\n", "")
        return setup_support.CommandResult(0, "", "")


def _repo(tmp_path: Path, *, lock: bool = True, requires: str = ">=3.11") -> Path:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text(f'requires-python = "{requires}"\n', encoding="utf-8")
    if lock:
        (backend / "requirements.lock").write_text("requests==2.32.3\n", encoding="utf-8")
    return tmp_path


def _touch_interpreter(venv_dir: Path) -> Path:
    path = setup_support.venv_python(venv_dir, "linux")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


def _run(tmp_path: Path, argv: list[str], runner: FakeRun, **kwargs):
    logs: list[str] = []
    removed: list[Path] = []

    def remove(path: Path) -> None:
        removed.append(path)
        setup_support.remove_path(path)

    code = setup_support.run_setup(
        argv,
        repo_root=tmp_path,
        run=runner,
        log=logs.append,
        platform="linux",
        remove=remove,
        which=kwargs.get("which", lambda _name: None),
    )
    return code, logs, removed, runner


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4", (4, 0, 0)),
        ("3.11", (3, 11, 0)),
        ("3.11.4", (3, 11, 4)),
        ("Python 3.12.3", (3, 12, 3)),
        ("  Python 3.13.1\n", (3, 13, 1)),
    ],
)
def test_parse_python_version(text: str, expected: tuple[int, int, int]) -> None:
    assert setup_support.parse_python_version(text).as_tuple() == expected


def test_parse_python_version_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        setup_support.parse_python_version("not a version")


@pytest.mark.parametrize(
    ("version", "spec", "matches"),
    [
        ((3, 11, 0), ">=3.11", True),
        ((3, 10, 12), ">=3.11", False),
        ((3, 12, 1), ">=3.11", True),
        ((3, 11, 0), ">=3.11.2", False),
        ((3, 12, 0), ">=3.11,<4", True),
        ((4, 0, 0), ">=3.11,<4", False),
        ((3, 11, 2), "==3.11.2", True),
    ],
)
def test_python_satisfies(version: tuple[int, int, int], spec: str, matches: bool) -> None:
    parsed = setup_support.Version(*version)
    assert setup_support.python_satisfies(parsed, spec) is matches


def test_python_satisfies_rejects_unknown_operator() -> None:
    version = setup_support.Version(3, 11, 0)
    with pytest.raises(ValueError, match="unsupported"):
        setup_support.python_satisfies(version, "~=3.11")


def test_read_requires_python_from_repo() -> None:
    text = setup_support.PYPROJECT_FILE.read_text(encoding="utf-8")
    assert setup_support.read_requires_python(text) == ">=3.11"


@pytest.mark.parametrize(
    ("exists", "force", "healthy", "action"),
    [
        (False, False, False, "create"),
        (False, True, False, "create"),
        (True, False, True, "reuse"),
        (True, True, True, "recreate"),
        (True, False, False, "recreate"),
    ],
)
def test_decide_venv_action(exists: bool, force: bool, healthy: bool, action: str) -> None:
    assert setup_support.decide_venv_action(exists=exists, force=force, healthy=healthy) == action


def test_venv_python_paths() -> None:
    root = Path("/tmp/example/.venv")
    assert setup_support.venv_python(root, "linux") == Path("/tmp/example/.venv/bin/python")
    assert setup_support.venv_python(root, "win32") == Path("/tmp/example/.venv/Scripts/python.exe")


def test_install_steps_use_the_lock_and_headless_extra(tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    lock.write_text("requests==1\n", encoding="utf-8")
    steps = setup_support.install_steps("/venv/bin/python", lock)
    labels = [label for label, _args in steps]
    assert labels == [
        "upgrading pip",
        "installing pinned dependencies (including headless)",
        "installing Playwright Chromium",
    ]
    install = steps[1][1]
    assert install[install.index("--constraint") + 1] == str(lock.resolve())
    assert ".[headless,dev]" in install
    assert steps[2][1][-2:] == ["install", "chromium"]
    for _label, args in steps:
        assert setup_support.command_blocked_host(args) is None


def test_self_check_is_offline_and_covers_import_help_pytest_and_chromium() -> None:
    steps = setup_support.self_check_steps("/venv/bin/python")
    blob = "\n".join(" ".join(args) for _label, args in steps)
    assert "import backend" in blob
    assert "--help" in blob
    assert "not integration" in blob
    assert "tests/test_csv_export.py" in blob
    assert "about:blank" in blob
    assert "http://" not in blob
    assert "https://" not in blob
    for host in setup_support.BLOCKED_HOSTS:
        assert host not in blob


def test_command_blocked_host_catches_racing_sites_only() -> None:
    assert setup_support.command_blocked_host(["curl", "https://race.netkeiba.com/top"]) == (
        "netkeiba.com"
    )
    assert (
        setup_support.command_blocked_host(["curl", "https://www.jra.go.jp/keiba/"]) == "jra.go.jp"
    )
    assert setup_support.command_blocked_host(["curl", "https://jra.jp/keiba/"]) == "jra.jp"
    assert (
        setup_support.command_blocked_host(["python", "-m", "playwright", "install", "chromium"])
        is None
    )


def test_chromium_snippet_opens_about_blank_only() -> None:
    snippet = setup_support.CHROMIUM_OFFLINE_SNIPPET
    assert 'page.goto("about:blank")' in snippet
    assert "http://" not in snippet
    assert "https://" not in snippet


def test_requirement_name_normalizes_extras_and_specifiers() -> None:
    assert setup_support.requirement_name("Requests>=2.31.0") == "requests"
    assert setup_support.requirement_name("python_dateutil==3.0.0") == "python-dateutil"
    assert setup_support.requirement_name("uvicorn[standard]>=0.27") == "uvicorn"


def test_lock_pins_every_direct_dependency() -> None:
    pyproject = setup_support.PYPROJECT_FILE.read_text(encoding="utf-8")
    lock_text = setup_support.LOCK_FILE.read_text(encoding="utf-8")
    data = tomllib.loads(pyproject)
    names = [setup_support.requirement_name(item) for item in data["project"]["dependencies"]]
    for extra in data["project"]["optional-dependencies"].values():
        names.extend(setup_support.requirement_name(item) for item in extra)
    pins = setup_support.lock_pins(lock_text)
    assert sorted(set(names) - set(pins)) == []
    assert pins["playwright"]
    assert pins["pytest"]


def test_lock_lines_are_exact_pins() -> None:
    for line in setup_support.LOCK_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert ">=" not in stripped
        assert "==" in stripped
        assert setup_support.lock_pins(stripped)


def test_format_command_hides_inline_scripts() -> None:
    shown = setup_support.format_command(
        ["python", "-c", "import backend\npage.goto('https://jra.jp/')", "-m", "pytest"]
    )
    assert shown == "python -c <script> -m pytest"
    assert "jra.jp" not in shown


def test_activate_hint_matches_platform() -> None:
    assert setup_support.activate_hint("linux").startswith("source ")
    assert "Scripts" in setup_support.activate_hint("win32")


def test_setup_rejects_an_old_python_before_creating_a_venv(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = FakeRun(versions={sys.executable: "3.10.4"})
    code, logs, removed, runner = _run(repo, [], runner)
    assert code == 1
    assert removed == []
    assert any("3.10.4" in line and line.startswith("[setup] FAIL") for line in logs)
    assert not any("-m" in call and "venv" in call for call in runner.calls)


def test_setup_creates_installs_and_self_checks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = FakeRun()
    code, logs, removed, runner = _run(repo, [], runner)
    assert code == 0
    assert removed == []
    flat = [" ".join(call) for call in runner.calls]
    assert any("-m venv " in call for call in flat)
    assert any("--constraint" in call and ".[headless,dev]" in call for call in flat)
    assert any("playwright install chromium" in call for call in flat)
    assert any("import backend" in call for call in flat)
    assert any("backend.cli --help" in call for call in flat)
    assert any("not integration" in call and "test_csv_export.py" in call for call in flat)
    assert any("about:blank" in call for call in flat)
    for call in runner.calls:
        assert setup_support.command_blocked_host(call) is None
    install_index = next(i for i, call in enumerate(flat) if "--constraint" in call)
    assert runner.cwds[install_index] == repo / "backend"
    assert any(line == "[setup] OK   setup complete" for line in logs)
    assert any("node not found" in line for line in logs)
    assert any(line.startswith("[setup] note activate with:") for line in logs)


def test_setup_reuses_a_healthy_venv(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    interpreter = _touch_interpreter(repo / "backend" / ".venv")
    runner = FakeRun(versions={sys.executable: "3.12.3", str(interpreter): "3.12.3"})
    code, logs, removed, runner = _run(repo, [], runner)
    assert code == 0
    assert removed == []
    assert interpreter.is_file()
    assert not any(" -m venv " in " ".join(call) for call in runner.calls)
    assert any("reusing backend/.venv" in line for line in logs)
    assert any("--constraint" in " ".join(call) for call in runner.calls)


def test_force_recreates_an_existing_venv(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    interpreter = _touch_interpreter(repo / "backend" / ".venv")
    marker = interpreter.parent / "keep.txt"
    marker.write_text("x", encoding="utf-8")
    runner = FakeRun()
    code, logs, removed, _runner = _run(repo, ["--force"], runner)
    assert code == 0
    assert removed == [repo / "backend" / ".venv"]
    assert not marker.exists()
    assert any("recreating backend/.venv" in line for line in logs)


def test_broken_venv_is_recreated_without_force(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    venv_dir = repo / "backend" / ".venv"
    venv_dir.mkdir()
    (venv_dir / "leftover").write_text("x", encoding="utf-8")
    runner = FakeRun()
    code, logs, removed, runner = _run(repo, [], runner)
    assert code == 0
    assert removed == [venv_dir]
    assert any("incomplete" in line for line in logs)
    assert any("-m venv " in " ".join(call) for call in runner.calls)


def test_incompatible_venv_python_is_recreated(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    interpreter = _touch_interpreter(repo / "backend" / ".venv")
    runner = FakeRun(versions={sys.executable: "3.12.3", str(interpreter): "3.10.0"})
    code, logs, removed, _runner = _run(repo, [], runner)
    assert code == 0
    assert removed == [repo / "backend" / ".venv"]
    assert any("3.10.0" in line for line in logs)


def test_pip_failure_stops_before_playwright(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = FakeRun(fail_substring="--constraint")
    code, logs, _removed, runner = _run(repo, [], runner)
    assert code == 1
    assert any("installing pinned dependencies" in line and "FAIL" in line for line in logs)
    assert not any("playwright" in " ".join(call) for call in runner.calls)


def test_self_check_failure_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = FakeRun(fail_substring="about:blank")
    code, logs, _removed, _runner = _run(repo, [], runner)
    assert code == 1
    assert any("headless Chromium" in line and "FAIL" in line for line in logs)
    assert not any(line == "[setup] OK   setup complete" for line in logs)


def test_missing_lock_refuses_unpinned_install(tmp_path: Path) -> None:
    repo = _repo(tmp_path, lock=False)
    runner = FakeRun()
    code, logs, _removed, runner = _run(repo, [], runner)
    assert code == 1
    assert any("requirements.lock" in line and "FAIL" in line for line in logs)
    assert not any("pip" in " ".join(call) and "install" in call for call in runner.calls)


def test_ensurepip_failure_mentions_the_debian_package(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = FakeRun(fail_substring="-m venv ")
    runner.stderr = (
        "The virtual environment was not created successfully because ensurepip is not available."
    )
    code, logs, _removed, _runner = _run(repo, [], runner)
    assert code == 1
    assert any("python3.12-venv" in line for line in logs)


def test_node_on_path_is_optional_and_does_not_fail_setup(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = FakeRun()
    code, logs, _removed, runner = _run(
        repo,
        [],
        runner,
        which=lambda name: "/usr/bin/node" if name == "node" else None,
    )
    assert code == 0
    assert any("/usr/bin/node --version" == " ".join(call) for call in runner.calls)
    assert any("node v22.14.0" in line for line in logs)


def test_remove_path_deletes_a_directory_and_a_file(tmp_path: Path) -> None:
    directory = tmp_path / "venv"
    directory.mkdir()
    (directory / "marker").write_text("x", encoding="utf-8")
    setup_support.remove_path(directory)
    assert not directory.exists()
    leftover = tmp_path / "stray"
    leftover.write_text("x", encoding="utf-8")
    setup_support.remove_path(leftover)
    assert not leftover.exists()


def test_parse_options_force_and_python() -> None:
    options = setup_support.parse_options(["--force", "--python", "/usr/bin/python3.11"])
    assert options.force is True
    assert options.python == "/usr/bin/python3.11"
    assert setup_support.parse_options([]).force is False
