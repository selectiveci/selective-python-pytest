import errno
import json
import os
import platform
import queue
import shutil
import subprocess
import threading
from pathlib import Path

from selective_pytest.environ import SelectiveError

RELEASE_URL = "https://github.com/selectiveci/transport/releases"
BUNDLED = Path(__file__).resolve().parent / "bin" / "transport"


class ConnectionLost(Exception):
    pass


_CLOSED = object()


def target_triple():
    return platform.system().lower() + "-" + platform.machine().lower()


def missing_binary_message():
    return (
        "Selective transport binary not found. This selective-pytest install has no transport for "
        + target_triple()
        + ", SELECTIVE_TRANSPORT is not set, and there is no transport on PATH. "
        + "Install a selective-pytest platform wheel, or set SELECTIVE_TRANSPORT to a transport with --stdio. See "
        + RELEASE_URL
    )


def resolve_binary():
    configured = os.environ.get("SELECTIVE_TRANSPORT", "").strip()
    if configured:
        if not os.path.isfile(configured):
            raise SelectiveError("SELECTIVE_TRANSPORT is " + configured + ", which does not exist.")
        return configured
    if BUNDLED.is_file():
        return str(BUNDLED)
    found = shutil.which("transport")
    if found is None:
        raise SelectiveError(missing_binary_message())
    return found


def check_stdio(binary):
    try:
        proc = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise SelectiveError(_cannot_execute(binary, exc)) from exc
    output = proc.stdout + proc.stderr
    if "--stdio" in output:
        return
    if "usage" in output.lower():
        raise SelectiveError(
            "Selective transport at "
            + binary
            + " has no --stdio mode; it is older than selective-pytest needs. See "
            + RELEASE_URL
        )
    detail = output.strip().splitlines()[0] if output.strip() else "exit status " + str(proc.returncode)
    raise SelectiveError("Selective transport at " + binary + " cannot run: " + detail)


def _cannot_execute(binary, exc):
    reason = exc.strerror or str(exc)
    if exc.errno == errno.ENOENT and os.path.isfile(binary):
        reason += "; the file exists, so the loader it names is missing (a glibc build cannot run on musl systems such as Alpine)"
    return "Selective transport at " + binary + " cannot be executed: " + reason


class StdioTransport:
    def __init__(self, proc):
        self.proc = proc
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.closed = False
        self._stdout = threading.Thread(target=self._read_stdout, daemon=True)
        self._stdout.start()

    @classmethod
    def open(cls, binary, url):
        env = os.environ.copy()
        env["SELECTIVE_TRANSPORT_URL"] = url
        proc = subprocess.Popen(
            [binary, "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        return cls(proc)

    def write_line(self, line):
        with self.lock:
            self._write_unlocked(line)

    def _write_unlocked(self, line):
        if self.closed or self.proc.poll() is not None:
            raise ConnectionLost()
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except OSError:
            raise ConnectionLost()

    def write_json(self, payload):
        self.write_line(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def write_json_unlocked(self, payload):
        self._write_unlocked(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    def read(self, timeout=None):
        try:
            item = self.queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError()
        if item is _CLOSED:
            raise ConnectionLost()
        if item is None or item == "":
            return self.read(timeout=timeout)
        try:
            return json.loads(item)
        except json.JSONDecodeError as exc:
            raise SelectiveError("The Selective server sent a frame that is not JSON (" + str(exc) + "): " + item)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                self.queue.put(line.strip("\r\n"))
        finally:
            self.queue.put(_CLOSED)
