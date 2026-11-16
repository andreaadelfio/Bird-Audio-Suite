"""A connection left dead by a pause: the request waits for an answer that never comes.

A local server stands in for Supabase and keeps quiet on the first request, as a dead
connection does: the client gives up after its read timeout and asks again, once, on a new
connection. An insert is never asked twice (it would leave an empty observation behind).
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import requests

from bird_audio_suite import cloud
from bird_audio_suite.cloud import CloudConfig, SupabaseRest, SupabaseStorage

READ_TIMEOUT = 0.3


class QuietOnce(BaseHTTPRequestHandler):
    """Answers [] to everything; the first request of each method gets no answer in time."""

    def _answer(self) -> None:
        server = self.server
        with server.lock:
            server.seen.append((self.command, self.path.split("?")[0]))
            first = self.command not in server.answered
            server.answered.add(self.command)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if first:
            time.sleep(READ_TIMEOUT * 4)  # the client has given up by now
        body = json.dumps([]).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the client closed that connection

    do_GET = do_POST = do_PATCH = do_DELETE = _answer

    def log_message(self, *args) -> None:
        pass


class DeadConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietOnce)
        self.server.lock, self.server.seen, self.server.answered = threading.Lock(), [], set()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.config = CloudConfig(supabase_url=f"http://127.0.0.1:{self.server.server_port}", supabase_key="k")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_a_read_is_asked_again_at_once_on_a_new_connection(self) -> None:
        rest = SupabaseRest(self.config, timeout=(1, READ_TIMEOUT))
        started = time.monotonic()
        self.assertEqual(rest.get("clips", {"select": "id"}), [])
        self.assertEqual(self.server.seen, [("GET", "/rest/v1/clips")] * 2)
        self.assertLess(time.monotonic() - started, READ_TIMEOUT * 3)  # one wait, not the server's

    def test_the_inbox_listing_of_the_uploader_too(self) -> None:
        with mock.patch.object(cloud, "SUPABASE_TIMEOUT", (1, READ_TIMEOUT)):
            self.assertEqual(SupabaseStorage(self.config).list("inbox"), [])
        self.assertEqual(self.server.seen, [("POST", "/storage/v1/object/list/bird-audio")] * 2)

    def test_a_new_observation_is_never_inserted_twice(self) -> None:
        rest = SupabaseRest(self.config, timeout=(1, READ_TIMEOUT))
        self.server.answered.add("GET")  # the nearby clips answer at once: none
        with self.assertRaises(requests.exceptions.RequestException):
            rest.observation_for("Turdus merula", datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc))
        time.sleep(READ_TIMEOUT)  # a second insert would arrive by now
        self.assertEqual([seen for seen in self.server.seen if seen[0] == "POST"], [("POST", "/rest/v1/observations")])


if __name__ == "__main__":
    unittest.main()
