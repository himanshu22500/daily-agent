"""Run a written DSA solution against its test cases, outside this process (#138).

The daily DSA writer (``feed/dsa_daily.py``) keeps a problem only if its
optimal solution passes the test cases Claude wrote with it. That solution is
code a model wrote, so it is never imported or exec'd here: ``check_solution``
runs it in a child Python, and that is the only place it runs.

- ``sys.executable -I -S -B``: isolated mode (``PYTHON*`` variables ignored,
  no script directory, working directory or user site on the path), no
  ``site`` (so no site-packages: only the standard library imports, never this
  repo or the venv's packages), and no ``.pyc`` writes.
- An empty temp directory as the working directory, ``HOME`` and ``TMPDIR``,
  and an environment with nothing else in it.
- A wall-clock timeout (the child is killed), plus CPU and memory limits.
- Before the solution is compiled, an audit hook refuses what a solution never
  needs: sockets (no network), subprocesses, ``ctypes``, signals, changing
  files, and opening any file outside the standard library.

That stops a careless or confused solution from doing harm. It is not a
sandbox for hostile code (an audit hook can be worked around). The child
reports each test's return value as JSON, and the comparison happens here.

A test is ``Case(args, expected)``: the entry function is called with
``*args``. For a class (a design problem, e.g. an LRU cache), ``args`` is the
list of calls ``[["LRUCache", [2]], ["put", [1, 1]], ["get", [1]]]``: the first
builds the object, and ``expected`` lists every call's return value (``None``
for the constructor), as LeetCode writes them.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field

FUNCTION, CLASS = "function", "class"
EXACT, ANY_ORDER = "exact", "any_order"
TIMEOUT_SECONDS = 10.0
MEMORY_BYTES = 1 << 30
MAX_OUTPUT_CHARS = 20_000
_MARK = "@@dsa-check@@"

# Runs in the child (``-c``): reads the job from stdin, limits itself, installs
# the audit hook, then runs the solution and prints one result line.
_HARNESS = r"""
import json, os, posixpath, sys

MARK = "@@dsa-check@@"
DENY = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
    "os.fork", "os.forkpty", "os.kill", "os.killpg", "os.startfile",
    "pty.spawn", "signal.pthread_kill", "os.remove", "os.rename", "os.rmdir",
    "os.mkdir", "os.chmod", "os.chown", "os.link", "os.symlink", "os.truncate",
    "os.utime", "os.putenv", "os.unsetenv", "os.chdir", "os.chflags",
    "os.lchflags", "os.setxattr", "os.removexattr", "shutil.rmtree",
    "webbrowser.open", "resource.setrlimit", "resource.prlimit",
}
DENY_PREFIXES = (
    "socket.", "ctypes.", "urllib.", "http.", "ftplib.", "smtplib.",
    "imaplib.", "poplib.", "nntplib.", "telnetlib.", "sqlite3.", "mmap.",
    "fcntl.", "winreg.", "_winapi.", "msvcrt.",
)
WRITES = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def limit(seconds, memory):
    try:
        import resource
    except ImportError:
        return
    for which, value in ((resource.RLIMIT_CPU, seconds), (resource.RLIMIT_AS, memory)):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            pass  # macOS refuses memory limits: the watchdog below covers it


def watchdog(memory, out):
    # Counts Python's own allocations (the OS's resident size undercounts on
    # macOS, which compresses memory).
    import threading, time, tracemalloc

    tracemalloc.start()

    def watch():
        while True:
            if tracemalloc.get_traced_memory()[0] > memory:
                fatal = "the solution used more than %d MiB of memory" % (memory >> 20)
                out.write(MARK + json.dumps({"fatal": fatal}) + "\n")
                out.flush()
                os._exit(3)
            time.sleep(0.01)

    threading.Thread(target=watch, daemon=True).start()


def guard():
    # The standard library's own directories: imports may read from them.
    roots = tuple(
        posixpath.normpath(p) + "/" for p in sys.path if p and posixpath.isabs(p)
    )
    cwd = os.getcwd()

    def hook(event, args):
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if isinstance(path, bytes):
                path = path.decode(errors="replace")
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WRITES
            )
            if (
                writing
                or not isinstance(path, str)
                or not posixpath.normpath(posixpath.join(cwd, path)).startswith(roots)
            ):
                raise PermissionError("a solution may not open " + repr(path))
        elif event in DENY or event.startswith(DENY_PREFIXES):
            raise PermissionError(event + " is not allowed in a solution")

    sys.addaudithook(hook)


def plain(value, depth=0):
    if depth > 100:
        raise ValueError("the return value is nested too deeply")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [plain(v, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((plain(v, depth + 1) for v in value), key=repr)
    if isinstance(value, dict):
        return {str(k): plain(v, depth + 1) for k, v in value.items()}
    raise TypeError("returned a " + type(value).__name__ + ", not a plain value")


def failure(exc):
    return (type(exc).__name__ + ": " + str(exc))[:300]


def run(target, kind, args):
    if kind == "function":
        return plain(target(*args))
    (_, first), *calls = args
    obj = target(*first)
    return [None] + [plain(getattr(obj, name)(*a)) for name, a in calls]


def main():
    job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    out = sys.stdout
    limit(job["cpu_seconds"], job["memory_bytes"])
    watchdog(job["memory_bytes"], out)

    class Sink:
        def write(self, text):
            return len(text)

        def flush(self):
            pass

    sys.stdout = Sink()  # the solution's prints go nowhere
    guard()
    ns = {"__name__": "solution"}
    try:
        exec(compile(job["code"], "solution.py", "exec"), ns)
        target = ns.get(job["entry"])
        if not callable(target):
            raise NameError("the solution defines no " + repr(job["entry"]))
    except BaseException as exc:
        out.write(MARK + json.dumps({"load_error": failure(exc)}) + "\n")
        out.flush()
        return
    results = []
    for args in job["tests"]:
        try:
            got = json.dumps(run(target, job["kind"], args))
            if len(got) > job["max_output"]:
                results.append({"error": "the return value is too large"})
            else:
                results.append({"got": got})
        except BaseException as exc:
            results.append({"error": failure(exc)})
    out.write(MARK + json.dumps({"results": results}) + "\n")
    out.flush()


main()
"""


@dataclass(frozen=True)
class Case:
    """One test: the entry's positional arguments (or a class's calls) and
    what it must return."""

    args: list
    expected: object


@dataclass
class CheckResult:
    total: int
    passed: int = 0
    failures: list[str] = field(default_factory=list)
    # The whole run failed: no tests, a syntax error, a crash, a timeout.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.total > 0 and self.passed == self.total

    def summary(self) -> str:
        if self.error:
            return self.error
        if self.ok:
            return f"all {self.total} tests passed"
        return f"{self.passed}/{self.total} tests passed; " + "; ".join(
            self.failures[:3]
        )


def _short(value: object, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def same(got: object, expected: object) -> bool:
    """Equal as JSON values; numbers within 1e-6 when either is a float."""
    if isinstance(got, bool) or isinstance(expected, bool):
        return type(got) is type(expected) and got == expected
    if isinstance(got, (int, float)) and isinstance(expected, (int, float)):
        if isinstance(got, float) or isinstance(expected, float):
            return math.isclose(got, expected, rel_tol=1e-6, abs_tol=1e-6)
        return got == expected
    if isinstance(got, list) and isinstance(expected, list):
        return len(got) == len(expected) and all(map(same, got, expected))
    if isinstance(got, dict) and isinstance(expected, dict):
        return got.keys() == expected.keys() and all(
            same(got[k], expected[k]) for k in expected
        )
    return type(got) is type(expected) and got == expected


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def matches(got: object, expected: object, compare: str = EXACT) -> bool:
    """``any_order``: a returned list may list its items in any order."""
    if compare == ANY_ORDER and isinstance(got, list) and isinstance(expected, list):
        return same(sorted(got, key=_canonical), sorted(expected, key=_canonical))
    return same(got, expected)


def _child_env(workdir: str) -> dict[str, str]:
    return {"HOME": workdir, "TMPDIR": workdir, "PATH": os.defpath}


# How the OS ends a child at its CPU limit: SIGXCPU at the soft limit,
# SIGKILL at the hard one (Linux). Either way it ran too long.
_CPU_KILLED = tuple(
    -getattr(signal, name) for name in ("SIGKILL", "SIGXCPU") if hasattr(signal, name)
)


async def check_solution(
    code: str,
    cases: Sequence[Case],
    *,
    entry: str,
    kind: str = FUNCTION,
    compare: str = EXACT,
    timeout: float = TIMEOUT_SECONDS,
) -> CheckResult:
    """Run ``code`` against ``cases`` in an isolated child Python."""
    result = CheckResult(total=len(cases))
    if not cases:
        result.error = "the solution came with no tests"
        return result
    if kind not in (FUNCTION, CLASS):
        result.error = f"unknown entry kind {kind!r}"
        return result
    job = {
        "code": code,
        "entry": entry,
        "kind": kind,
        "tests": [case.args for case in cases],
        # A second over the wall clock, so the timeout below normally fires
        # first (Linux kills at the CPU limit, macOS rarely gets there).
        "cpu_seconds": max(1, math.ceil(timeout)) + 1,
        "memory_bytes": MEMORY_BYTES,
        "max_output": MAX_OUTPUT_CHARS,
    }
    with tempfile.TemporaryDirectory(prefix="daily-agent-dsa-") as workdir:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            _HARNESS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=_child_env(workdir),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(job).encode()), timeout
            )
        except asyncio.TimeoutError:
            result.error = f"the solution timed out after {timeout:.0f}s"
            return result
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    lines = [
        line
        for line in stdout.decode(errors="replace").splitlines()
        if line.startswith(_MARK)
    ]
    if not lines and proc.returncode in _CPU_KILLED:
        result.error = f"the solution timed out after {timeout:.0f}s"
        return result
    if not lines:
        detail = stderr.decode(errors="replace").strip().splitlines()
        result.error = f"the solution crashed (exit {proc.returncode})" + (
            f": {detail[-1][:200]}" if detail else ""
        )
        return result
    report = json.loads(lines[-1][len(_MARK) :])
    if "fatal" in report:
        result.error = report["fatal"]
        return result
    if "load_error" in report:
        result.error = f"the solution doesn't load: {report['load_error']}"
        return result
    for number, (case, outcome) in enumerate(zip(cases, report["results"]), 1):
        where = f"test {number} (input {_short(case.args)})"
        if "error" in outcome:
            result.failures.append(f"{where} raised {outcome['error']}")
            continue
        got = json.loads(outcome["got"])
        if matches(got, case.expected, compare):
            result.passed += 1
        else:
            result.failures.append(
                f"{where}: expected {_short(case.expected)}, got {_short(got)}"
            )
    return result
