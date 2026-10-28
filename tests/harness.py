import base64
import gzip
import hashlib
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

import pytest

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def transport_bin():
    explicit = os.environ.get("SELECTIVE_TRANSPORT", "")
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        return explicit
    default = "/tmp/transport/target/release/transport"
    if os.path.isfile(default) and os.access(default, os.X_OK):
        return default
    pytest.skip("transport binary is not built")


def pop_frame(buf):
    if len(buf) < 2:
        return None, buf
    first = buf[0]
    second = buf[1]
    opcode = first & 0x0F
    masked = (second & 0x80) != 0
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buf) < 4:
            return None, buf
        length = struct.unpack("!H", bytes(buf[2:4]))[0]
        offset = 4
    elif length == 127:
        if len(buf) < 10:
            return None, buf
        length = struct.unpack("!Q", bytes(buf[2:10]))[0]
        offset = 10
    mask_len = 4 if masked else 0
    total = offset + mask_len + length
    if len(buf) < total:
        return None, buf
    mask = bytes(buf[offset : offset + mask_len]) if masked else b""
    start = offset + mask_len
    payload = bytes(buf[start : start + length])
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return (opcode, payload), buf[total:]


def send_frame(conn, opcode, payload):
    size = len(payload)
    header = bytearray([0x80 | opcode])
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header.extend(struct.pack("!H", size))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", size))
    conn.sendall(bytes(header) + payload)


class MockServer:
    def __init__(self):
        self.inbox = queue.Queue()
        self.outbox = queue.Queue()
        self.uris = []
        self.http_requests = []
        self.generation = 0
        self._conn = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(32)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.generation += 1
        try:
            self._sock.close()
        except OSError:
            pass
        self.drop()

    def drop(self):
        with self._lock:
            self.generation += 1
            conn = self._conn
            self._conn = None
            self._drain_inbox()
            self._drain_outbox()
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def _drain_inbox(self):
        while True:
            try:
                self.inbox.get_nowait()
            except queue.Empty:
                break

    def _drain_outbox(self):
        while True:
            try:
                self.outbox.get_nowait()
            except queue.Empty:
                break

    def send(self, message):
        self.outbox.put(message)

    def _accept_loop(self):
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, rest = data.split(b"\r\n\r\n", 1)
            text = head.decode("iso-8859-1")
            if text.startswith("POST "):
                length = 0
                for header in text.split("\r\n"):
                    if header.lower().startswith("content-length:"):
                        length = int(header.split(":", 1)[1].strip())
                body = rest
                while len(body) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    body += chunk
                self.http_requests.append((text, body))
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            parts = text.split("\r\n", 1)[0].split(" ")
            uri = parts[1] if len(parts) > 1 else ""
            self.uris.append(uri)
            key = ""
            for header in text.split("\r\n")[1:]:
                if header.lower().startswith("sec-websocket-key:"):
                    key = header.split(":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((key + GUID).encode("ascii")).digest()
            ).decode("ascii")
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Accept: " + accept + "\r\n\r\n"
                ).encode("ascii")
            )
            generation = self.generation
            with self._lock:
                self._conn = conn
            self.inbox.put({"__connected__": uri})
            self._io(conn, generation, rest)
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _io(self, conn, generation, initial):
        buf = bytearray(initial)
        conn.settimeout(0.1)
        while generation == self.generation and not self._stop.is_set():
            while generation == self.generation:
                try:
                    message = self.outbox.get_nowait()
                except queue.Empty:
                    break
                send_frame(conn, 0x1, json.dumps(message).encode("utf-8"))
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                frame, buf = pop_frame(buf)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    send_frame(conn, 0xA, payload)
                    continue
                if opcode == 0x1:
                    decoded = json.loads(payload.decode("utf-8"))
                    with self._lock:
                        if generation != self.generation:
                            return
                        self.inbox.put(decoded)


def client_env(server, **extra):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SELECTIVE_"):
            env.pop(key)
    env.update(
        {
            "SELECTIVE_API_KEY": "conformance-key",
            "SELECTIVE_HOST": "ws://127.0.0.1:" + str(server.port),
            "SELECTIVE_PLATFORM": "github_actions",
            "SELECTIVE_RUN_ID": "run-1",
            "SELECTIVE_RUN_ATTEMPT": "1",
            "SELECTIVE_BRANCH": "main",
            "SELECTIVE_SHA": "abc123",
            "SELECTIVE_GIT_REPO": "selectiveci/selective-python-pytest",
            "SELECTIVE_GIT_PROVIDER": "github",
            "SELECTIVE_RUNNER_ID": "runner-1",
            "SELECTIVE_CONFORMANCE_FAST": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parent / "fast_retries"),
            "SELECTIVE_TRANSPORT": transport_bin(),
            "SELECTIVE_CONFORMANCE_IDS": "1",
        }
    )
    env.update(extra)
    return env


def spawn_client(cwd, args, env):
    proc = subprocess.Popen(
        [sys.executable, "-m", "selective_pytest", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout = []
    stderr = []
    threading.Thread(target=_drain, args=(proc.stdout, stdout), daemon=True).start()
    threading.Thread(target=_drain, args=(proc.stderr, stderr), daemon=True).start()
    proc.stdout_lines = stdout
    proc.stderr_lines = stderr
    return proc


def _drain(pipe, bucket):
    if pipe is None:
        return
    for line in pipe:
        bucket.append(line)


class Player:
    def __init__(self, server, proc):
        self.server = server
        self.proc = proc
        self.frames = []
        self.collected = set()

    def play(self, steps):
        for step in steps:
            self.step(step)

    def step(self, step):
        op = step["op"]
        if op == "send":
            self.ensure()
            self.server.send(step["message"])
            return None
        if op == "expect_manifest":
            frame = self.next_frame()
            self._expect_manifest(frame, step.get("tree", False), step.get("ids"))
            return frame
        if op == "expect_result":
            frame = self.next_frame()
            self._expect_result(frame, step["id"], step["status"])
            return frame
        if op == "expect_report":
            frame = self.next_frame()
            self._expect_report(frame, step["connection_retries"])
            return frame
        if op == "expect_exit":
            self.expect_exit(step["code"], step.get("timeout", 15))
            return None
        if op == "expect_reconnect":
            self.expect_reconnect()
            return None
        if op == "drop":
            self.server.drop()
            self.frames.clear()
            return None
        if op == "signal_term":
            os.kill(self.proc.pid, 15)
            return None
        if op == "expect_terminating":
            frame = self.next_frame()
            assert frame.get("type") == "terminating", frame
            assert frame.get("data", {}).get("signal") == step["signal"], frame
            return frame
        if op == "expect_fragment":
            return self.expect_fragment(step["ids"])
        raise AssertionError("unknown op " + op)

    def _wait_for(self, ready, what, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if ready():
                return
            time.sleep(0.05)
        raise AssertionError(self._debug("timed out waiting for " + what))

    def expect_fragment(self, ids):
        self._wait_for(lambda: self.server.http_requests, "the fragment POST", 5)
        header, body = self.server.http_requests[0]
        request_line = header.split("\r\n", 1)[0]
        lowered = header.lower()
        assert "authorization: conformance-key" in lowered, header
        assert "content-type: application/gzip" in lowered, header
        assert urlparse(request_line.split(" ")[1]).path == "/test_maps/fragments", request_line
        query = parse_qs(urlparse(request_line.split(" ")[1]).query)
        assert query == {"run_id": ["run-1"], "run_attempt": ["1"], "runner_id": ["runner-1"]}, request_line
        fragment = json.loads(gzip.decompress(body))
        assert fragment["version"] == 1 and isinstance(fragment["files"], list), fragment
        assert [test["id"] for test in fragment["tests"]] == ids, fragment
        return fragment

    def ensure(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.server.uris:
                return
            try:
                event = self.server.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(event, dict) and event.get("__connected__"):
                return
            self.frames.append(event)
        raise AssertionError(self._debug("timed out waiting for the client to connect"))

    def next_frame(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.frames:
                frame = self.frames.pop(0)
                if isinstance(frame, dict) and frame.get("__connected__"):
                    continue
                return frame
            try:
                event = self.server.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(event, dict) and event.get("__connected__"):
                continue
            return event
        raise AssertionError(self._debug("timed out waiting for a client frame"))

    def expect_reconnect(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            for uri in self.server.uris:
                if "reconnect=true" in uri or "reconnect=True" in uri:
                    return
            time.sleep(0.05)
        raise AssertionError(self._debug("timed out waiting for reconnect=true"))

    def expect_exit(self, code, timeout=15):
        try:
            status = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise AssertionError(self._debug("timed out waiting for exit " + str(code)))
        assert status == code, self._debug("exit " + str(status) + " expected " + str(code))

    def _expect_manifest(self, frame, tree, ids):
        assert frame.get("type") == "test_manifest", frame
        cases = frame["data"]["test_cases"]
        got = [case["id"] for case in cases]
        self.collected = set(got)
        if ids is not None:
            assert got == ids, got
        for case in cases:
            assert "run_time" in case, case
            assert isinstance(case["run_time"], (int, float)), case
            assert case.get("file_path"), case
        tree_value = frame["data"].get("test_map_tree")
        if tree:
            assert isinstance(tree_value, dict), frame
            for key in ("sha", "prefix", "files", "dirty"):
                assert key in tree_value, tree_value
        else:
            assert tree_value is None, frame

    def _expect_result(self, frame, test_id, status):
        assert frame.get("type") == "test_case_result", frame
        data = frame["data"]
        assert data["id"] == test_id, data
        assert data["status"] == status, data
        assert "run_time" in data
        assert data.get("file_path")
        if status == "failed":
            backtrace = data.get("failure_formatted_backtrace")
            if test_id in self.collected:
                assert backtrace and str(backtrace[0]).startswith("tests/"), data
            else:
                assert backtrace == [] and data["file_path"] == test_id.split("::")[0], data
            assert data.get("failure_message_lines")

    def _expect_report(self, frame, retries):
        assert frame.get("type") == "report_at_finish", frame
        assert frame["data"]["connection_retries"] == retries, frame

    def _debug(self, message):
        return (
            message
            + "\nstdout:\n"
            + "".join(self.proc.stdout_lines)
            + "\nstderr:\n"
            + "".join(self.proc.stderr_lines)
        )


def write_conformance_suite(root, flake_file, finish_mark):
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "conftest.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "ORDER = ['case-pass', 'case-fail', 'case-pend', 'case-flake']\n"
        "\n"
        "def pytest_collection_modifyitems(items):\n"
        "    if os.environ.get('SELECTIVE_CONFORMANCE_IDS') != '1':\n"
        "        return\n"
        "    by_name = {item.name: item for item in items}\n"
        "    ordered = []\n"
        "    for name in ORDER:\n"
        "        item = by_name['test_' + name.replace('-', '_')]\n"
        "        item._nodeid = name\n"
        "        if hasattr(item, '_id'):\n"
        "            item._id = type(item._id).parse(name)\n"
        "        ordered.append(item)\n"
        "    items[:] = ordered\n"
        "\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    path = os.environ.get('SELECTIVE_FINISH_MARK')\n"
        "    if path:\n"
        "        Path(path).write_text('yes')\n"
    )
    (tests / "test_cases.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "def test_case_pass():\n"
        "    assert True\n"
        "\n"
        "def test_case_fail():\n"
        "    assert False\n"
        "\n"
        "def test_case_pend():\n"
        "    pytest.skip('skipped')\n"
        "\n"
        "def test_case_flake():\n"
        "    path = Path(os.environ['SELECTIVE_FLAKE_FILE'])\n"
        "    count = int(path.read_text() or '0') if path.exists() else 0\n"
        "    path.write_text(str(count + 1))\n"
        "    assert count >= 1\n"
    )
    (root / "pytest.ini").write_text("[pytest]\n")
    return {
        "SELECTIVE_FLAKE_FILE": str(flake_file),
        "SELECTIVE_FINISH_MARK": str(finish_mark),
    }


def init_git(path):
    subprocess.check_call(["git", "init"], cwd=path, stdout=subprocess.DEVNULL)
    subprocess.check_call(
        ["git", "config", "user.email", "tests@example.com"], cwd=path
    )
    subprocess.check_call(["git", "config", "user.name", "tests"], cwd=path)
    subprocess.check_call(["git", "add", "."], cwd=path)
    subprocess.check_call(
        ["git", "commit", "-m", "init"], cwd=path, stdout=subprocess.DEVNULL
    )
