"""Local stand-in for Supabase to try the review site before the release.

    make web-dev            # or: python dev/local_stack.py --sync

Starts an embedded Postgres (pgserver) with birds_schema.sql, PostgREST on top of
it (the same REST layer Supabase uses), and one HTTP server on http://127.0.0.1:8010 that
serves docs/, proxies /rest/v1 to PostgREST, serves clips and spectrograms under /media
and generates assets/config.js with local tokens. --sync first pushes the local
detections into it with `bird_audio_cli.py sync`.

Everything lives in ~/.cache/bird-audio-suite/local-supabase and is disposable.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bird_audio_suite.cloud import DEFAULT_CACHE_DIR  # noqa: E402

STACK_DIR = DEFAULT_CACHE_DIR / "local-supabase"
POSTGREST_VERSION = "v12.2.3"
POSTGREST_URL = (
    f"https://github.com/PostgREST/postgrest/releases/download/{POSTGREST_VERSION}/"
    f"postgrest-{POSTGREST_VERSION}-linux-static-x64.tar.xz"
)
JWT_SECRET = "local-only-bird-audio-suite-jwt-secret-0123456789"
POSTGREST_PORT = 3010


def make_jwt(claims: dict) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({**claims, "exp": int(time.time()) + 30 * 86400}).encode())
    signature = hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(signature)}"


ANON_TOKEN = make_jwt({"role": "anon"})
SERVICE_TOKEN = make_jwt({"role": "service_role"})


def ensure_postgrest() -> Path:
    binary = STACK_DIR / "bin" / "postgrest"
    if binary.exists():
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    archive = binary.parent / "postgrest.tar.xz"
    print(f"Scarico PostgREST {POSTGREST_VERSION}...")
    urllib.request.urlretrieve(POSTGREST_URL, archive)
    with tarfile.open(archive) as handle:
        handle.extract("postgrest", binary.parent)
    archive.unlink()
    binary.chmod(0o755)
    return binary


def start_database():
    import pgserver

    STACK_DIR.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(STACK_DIR / "pgdata"), cleanup_mode="stop")
    for sql_file in (PROJECT_ROOT / "dev" / "local_supabase_stubs.sql", PROJECT_ROOT / "supabase" / "birds_schema.sql"):
        output = server.psql(sql_file.read_text(encoding="utf-8"))
        if "ERROR" in output:
            raise RuntimeError(f"{sql_file.name}:\n{output}")
    return server


def start_postgrest(server) -> subprocess.Popen:
    socket_dir = server.get_uri().split("host=", 1)[1]
    config = STACK_DIR / "postgrest.conf"
    config.write_text(
        "\n".join(
            [
                f'db-uri = "postgres://authenticator:local-authenticator@/postgres?host={socket_dir}"',
                'db-schemas = "birds"',
                'db-anon-role = "anon"',
                f'jwt-secret = "{JWT_SECRET}"',
                f"server-port = {POSTGREST_PORT}",
                'server-host = "127.0.0.1"',
            ]
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen([str(ensure_postgrest()), str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{POSTGREST_PORT}/", timeout=1)
            return process
        except urllib.error.HTTPError:
            return process  # it answers: ready
        except OSError:
            time.sleep(0.2)
    process.terminate()
    raise RuntimeError("PostgREST non parte")


def run_sync(detections_dir: Path, base_url: str) -> None:
    env_file = STACK_DIR / "cloud.env"
    env_file.write_text(
        f"SUPABASE_URL={base_url}\nSUPABASE_KEY={SERVICE_TOKEN}\nMEDIA_STORAGE=local\nMEDIA_BASE_URL=media\n",
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "bird_audio_cli.py"), "sync", "--config", str(env_file), "--detections-dir", str(detections_dir)],
        check=False,
    )


class Handler(SimpleHTTPRequestHandler):
    web_dir = PROJECT_ROOT / "docs"
    detections_dir = PROJECT_ROOT / "detections"
    spectrograms_dir = DEFAULT_CACHE_DIR / "spectrograms"

    def translate_path(self, path: str) -> str:
        clean = urllib.request.url2pathname(path.split("?", 1)[0])
        for prefix, root in (("/media/clips/", self.detections_dir), ("/media/spectrograms/", self.spectrograms_dir)):
            if clean.startswith(prefix):
                return str(root / clean[len(prefix):])
        return str(self.web_dir / clean.lstrip("/"))

    def do_GET(self) -> None:
        if self.path.startswith("/rest/v1/"):
            return self.proxy()
        if self.path.split("?", 1)[0] == "/assets/config.js":
            body = (
                "window.APP_CONFIG = "
                + json.dumps(
                    {
                        "supabaseUrl": f"http://{self.headers['Host']}",
                        "supabaseKey": ANON_TOKEN,
                        "schema": "birds",
                    }
                )
                + ";\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def do_POST(self) -> None:
        self.proxy() if self.path.startswith("/rest/v1/") else self.send_error(405)

    do_PATCH = do_POST
    do_DELETE = do_POST

    def do_HEAD(self) -> None:
        # supabase-js counts rows with HEAD (select(..., { head: true }))
        self.proxy() if self.path.startswith("/rest/v1/") else super().do_HEAD()

    def do_OPTIONS(self) -> None:
        self.proxy() if self.path.startswith("/rest/v1/") else self.send_error(405)

    def proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {key: value for key, value in self.headers.items() if key.lower() not in {"host", "content-length", "apikey"}}
        request = urllib.request.Request(
            f"http://127.0.0.1:{POSTGREST_PORT}{self.path[len('/rest/v1'):]}",
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, response_headers, payload = response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            status, response_headers, payload = error.code, error.headers, error.read()
        self.send_response(status)
        for key, value in response_headers.items():
            if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        if os.environ.get("BIRD_DEV_VERBOSE"):
            super().log_message(format, *args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--sync", action="store_true", help="Push local detections before serving.")
    parser.add_argument("--reset", action="store_true", help="Delete the local database first.")
    parser.add_argument("--detections-dir", type=Path, default=PROJECT_ROOT / "detections")
    args = parser.parse_args()

    if args.reset:
        shutil.rmtree(STACK_DIR / "pgdata", ignore_errors=True)
        (DEFAULT_CACHE_DIR / "sync_state.json").unlink(missing_ok=True)
    server = start_database()
    postgrest = start_postgrest(server)
    Handler.detections_dir = args.detections_dir
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    base_url = f"http://127.0.0.1:{args.port}"
    try:
        if args.sync:
            import threading

            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            run_sync(args.detections_dir, base_url)
        print(f"Sito di revisione: {base_url}/  (Ctrl+C per fermare)")
        if args.sync:
            threading.Event().wait()
        else:
            httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown() if args.sync else None
        postgrest.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
