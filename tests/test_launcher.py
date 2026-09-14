"""Launcher integration checks; uses a tiny local stub, never a paid API."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
STUB = '''import argparse, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, required=True)
args = parser.parse_args()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'ok': True, 'app': 'tingji'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        self.send_response(204)
        self.end_headers()
        threading.Thread(target=server.shutdown, daemon=True).start()
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
server.serve_forever()
server.server_close()
'''


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(POWERSHELL and os.name == "nt", "Windows PowerShell is required")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingji launch & 中文 ")
        self.root = Path(self.temp.name)
        self.data = self.root / "隔离 data & logs"
        self.port = unused_port()
        shutil.copy2(PROJECT_ROOT / "launcher.ps1", self.root / "launcher.ps1")
        (self.root / "server.py").write_text(STUB, encoding="utf-8")
        self.env = dict(os.environ, TINGJI_DATA_DIR=str(self.data))
        self.started = False

    def tearDown(self):
        if self.started or (self.data / "server.pid").exists():
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/__test_shutdown", method="POST")
                with urllib.request.urlopen(req, timeout=2):
                    pass
            except OSError:
                pass
            # Give the detached stub time to release its logs before deleting its temp folder.
            for _ in range(40):
                with socket.socket() as sock:
                    if sock.connect_ex(("127.0.0.1", self.port)) != 0:
                        break
                time.sleep(0.1)
        self.temp.cleanup()

    def launch(self):
        # A detached Windows child may retain a parent's anonymous pipe handles.
        # File-backed output lets this check wait for the launcher alone.
        output_path = self.root / "launcher-test-output.log"
        with output_path.open("wb") as output:
            result = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(self.root / "launcher.ps1"), "-NoBrowser", "-Port", str(self.port)],
                cwd=self.root, env=self.env, stdout=output, stderr=output, timeout=22,
            )
        result.stderr = output_path.read_bytes()
        return result

    def test_launch_from_path_with_spaces_ampersand_and_chinese_then_reuse(self):
        result = self.launch()
        self.started = (self.data / "server.pid").exists()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=2) as response:
            self.assertEqual(json.load(response)["app"], "tingji")
        first_pid = (self.data / "server.pid").read_text()
        second = self.launch()
        self.assertEqual(second.returncode, 0, second.stderr.decode(errors="replace"))
        self.assertEqual((self.data / "server.pid").read_text(), first_pid)
        self.assertIn("Reusing healthy Tingji", (self.data / "launcher.log").read_text(encoding="utf-8"))

    def test_unrelated_service_is_not_reused_or_stopped(self):
        class OtherHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok":true,"app":"unrelated"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        other = ThreadingHTTPServer(("127.0.0.1", self.port), OtherHandler)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            result = self.launch()
            self.assertEqual(result.returncode, 1)
            self.assertFalse((self.data / "server.pid").exists())
            self.assertIn("正被其他程序占用", (self.data / "launcher.log").read_text(encoding="utf-8"))
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=2) as response:
                self.assertEqual(json.load(response)["app"], "unrelated")
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=2)

    def test_missing_server_leaves_useful_error_log(self):
        (self.root / "server.py").unlink()
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("找不到听记服务文件", (self.data / "launcher.log").read_text(encoding="utf-8"))
        self.assertFalse((self.data / "server.pid").exists())


if __name__ == "__main__":
    unittest.main()
