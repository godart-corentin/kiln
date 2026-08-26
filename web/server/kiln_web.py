#!/usr/bin/env python3
import json
import mimetypes
import os
import re
import stat
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

BUILDS = Path(os.environ.get("KILN_WEB_BUILDS", "/var/lib/kiln/builds"))
STATIC_ROOT = Path(os.environ.get("KILN_WEB_STATIC", "/opt/kiln/static"))
HOST = os.environ.get("KILN_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("KILN_WEB_PORT", "8088"))

BUILD_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
FINAL_BUILD_STATES = {"success", "failed", "aborted"}
FINAL_JOB_STATES = {"success", "failed", "skipped", "aborted"}
ANSI_RE = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1B\\))", re.DOTALL)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_LOG_CHUNK = 128 * 1024
MAX_BUILDS = 100
MAX_ARTIFACTS = 500
SSE_POLL_SECONDS = 0.35
SSE_KEEPALIVE_SECONDS = 15.0


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sanitize_log(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return CONTROL_RE.sub("", ANSI_RE.sub("", text))


def build_dirs():
    try:
        dirs = [path for path in BUILDS.iterdir() if path.is_dir() and BUILD_RE.fullmatch(path.name)]
    except (FileNotFoundError, PermissionError):
        return []
    return sorted(dirs, key=lambda path: path.name, reverse=True)[:MAX_BUILDS]


def get_build(build_id: str):
    if not BUILD_RE.fullmatch(build_id):
        return None
    path = BUILDS / build_id
    try:
        if not path.is_dir():
            return None
        status = read_json(path / "status.json")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if status.get("build_id") != build_id:
        return None
    return path, status


def api_builds():
    result = []
    for build_dir in build_dirs():
        try:
            status = read_json(build_dir / "status.json")
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if status.get("build_id") != build_dir.name:
            continue
        result.append({
            key: status.get(key)
            for key in (
                "build_id",
                "project",
                "sha",
                "ref",
                "type",
                "state",
                "created_at",
                "started_at",
                "finished_at",
                "duration_seconds",
            )
        })
    return result


def artifact_list(build_dir: Path):
    root = build_dir / "artifacts"
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []

    items = []
    try:
        for path in sorted(root.rglob("*")):
            if len(items) >= MAX_ARTIFACTS:
                break
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            items.append({"path": relative.as_posix(), "size": info.st_size})
    except OSError:
        return items
    return items


def log_snapshot(path: Path):
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - MAX_LOG_BYTES)
        handle.seek(start)
        data = handle.read()
    truncated = start > 0
    if truncated:
        first_newline = data.find(b"\n")
        if first_newline >= 0:
            data = data[first_newline + 1 :]
    return {
        "content": sanitize_log(data.decode("utf-8", errors="replace")),
        "offset": size,
        "truncated": truncated,
    }


def read_log_chunk(path: Path, offset: int):
    if offset < 0:
        raise ValueError("offset must be non-negative")
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if offset > size:
            offset = size
        handle.seek(offset)
        data = handle.read(MAX_LOG_CHUNK)
        next_offset = handle.tell()
    return {
        "offset": next_offset,
        "content": sanitize_log(data.decode("utf-8", errors="replace")),
    }


def pipeline_jobs(status: dict):
    pipeline = status.get("pipeline")
    if not isinstance(pipeline, dict):
        return {}
    jobs = pipeline.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def job_terminal(status: dict, job: str) -> bool:
    if job == "pipeline":
        return status.get("state") in FINAL_BUILD_STATES
    item = pipeline_jobs(status).get(job)
    return isinstance(item, dict) and item.get("state") in FINAL_JOB_STATES


def log_path_for(build_dir: Path, status: dict, job: str):
    if job == "pipeline":
        path = build_dir / "logs" / "pipeline.log"
    else:
        if not JOB_RE.fullmatch(job):
            return None
        item = pipeline_jobs(status).get(job)
        if not isinstance(item, dict):
            return None
        log_name = item.get("log")
        if isinstance(log_name, str) and log_name:
            candidate = build_dir / log_name
        else:
            candidate = build_dir / "logs" / f"{job}.log"
        try:
            logs_root = (build_dir / "logs").resolve()
            candidate = candidate.resolve()
            candidate.relative_to(logs_root)
        except (OSError, ValueError):
            return None
        path = candidate
    try:
        if path.is_symlink() or not path.is_file():
            return None
    except OSError:
        return None
    return path


def diff_status_events(previous: dict, current: dict):
    events = []
    prev_jobs = pipeline_jobs(previous)
    curr_jobs = pipeline_jobs(current)
    for name, job in curr_jobs.items():
        if not isinstance(job, dict):
            continue
        old = prev_jobs.get(name) if isinstance(prev_jobs.get(name), dict) else {}
        if job.get("state") != old.get("state"):
            data = {"name": name, "state": job.get("state")}
            if isinstance(job.get("duration_seconds"), (int, float)):
                data["duration_seconds"] = job["duration_seconds"]
            events.append(("job", data))
    if current.get("state") != previous.get("state"):
        data = {"state": current.get("state")}
        if isinstance(current.get("duration_seconds"), (int, float)):
            data["duration_seconds"] = current["duration_seconds"]
        events.append(("build", data))
    return events


def sse_encode(event: str, data: dict) -> bytes:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


class KilnHandler(BaseHTTPRequestHandler):
    server_version = "KilnWeb/2"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _security_headers(self, *, content_type: str):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'; object-src 'none'",
        )

    def send_bytes(self, code: int, data: bytes, content_type: str, *, cache: str = "no-store"):
        self.send_response(code)
        self._security_headers(content_type=content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, code: int, value):
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_bytes(code, data, "application/json; charset=utf-8")

    def send_error_json(self, code: int, message: str):
        self.send_json(code, {"error": message})

    def start_sse(self):
        self.send_response(200)
        self._security_headers(content_type="text/event-stream; charset=utf-8")
        # No Content-Length is possible for a live stream. Explicitly close the
        # upstream HTTP connection when the handler ends so proxies/clients can
        # detect the terminal SSE event without waiting for another response.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def write_sse(self, event: str, data: dict):
        self.wfile.write(sse_encode(event, data))
        self.wfile.flush()

    def write_keepalive(self):
        self.wfile.write(b": keepalive\n\n")
        self.wfile.flush()

    def serve_log_stream(self, build_dir: Path, status: dict, job: str, offset: int):
        path = log_path_for(build_dir, status, job)
        if path is None:
            self.send_error_json(404, "Log not found")
            return
        self.start_sse()
        last_keepalive = time.monotonic()
        try:
            while True:
                chunk = read_log_chunk(path, offset)
                if chunk["offset"] != offset:
                    offset = chunk["offset"]
                    self.write_sse("chunk", chunk)
                try:
                    current = read_json(build_dir / "status.json")
                except (OSError, ValueError, json.JSONDecodeError):
                    current = status
                if job_terminal(current, job):
                    # One final read closes the race between the final status write and log flush.
                    final_chunk = read_log_chunk(path, offset)
                    if final_chunk["offset"] != offset:
                        offset = final_chunk["offset"]
                        self.write_sse("chunk", final_chunk)
                    terminal_state = current.get("state") if job == "pipeline" else pipeline_jobs(current).get(job, {}).get("state")
                    self.write_sse("end", {"offset": offset, "state": terminal_state})
                    return
                now = time.monotonic()
                if now - last_keepalive >= SSE_KEEPALIVE_SECONDS:
                    self.write_keepalive()
                    last_keepalive = now
                time.sleep(SSE_POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            return

    def serve_build_events(self, build_dir: Path, initial: dict):
        self.start_sse()
        previous = initial
        last_keepalive = time.monotonic()
        try:
            while True:
                time.sleep(SSE_POLL_SECONDS)
                try:
                    current = read_json(build_dir / "status.json")
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                for event, data in diff_status_events(previous, current):
                    self.write_sse(event, data)
                previous = current
                if current.get("state") in FINAL_BUILD_STATES:
                    self.write_sse("end", {"state": current.get("state")})
                    return
                now = time.monotonic()
                if now - last_keepalive >= SSE_KEEPALIVE_SECONDS:
                    self.write_keepalive()
                    last_keepalive = now
        except (BrokenPipeError, ConnectionResetError):
            return

    def serve_static(self, url_path: str):
        if url_path == "/":
            candidate = STATIC_ROOT / "index.html"
            cache = "no-store"
        elif url_path.startswith("/assets/"):
            relative = url_path.removeprefix("/")
            candidate = STATIC_ROOT / relative
            cache = "public, max-age=31536000, immutable"
        else:
            candidate = STATIC_ROOT / "index.html"
            cache = "no-store"
        try:
            resolved = candidate.resolve()
            resolved.relative_to(STATIC_ROOT.resolve())
            if resolved.is_symlink() or not resolved.is_file():
                raise FileNotFoundError
            data = resolved.read_bytes()
        except (OSError, ValueError):
            self.send_error_json(404, "Not found")
            return
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_bytes(200, data, content_type, cache=cache)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)

        if path == "/healthz":
            self.send_bytes(200, b"ok\n", "text/plain; charset=utf-8")
            return

        if path == "/api/builds":
            self.send_json(200, {"builds": api_builds()})
            return

        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[:2] == ["api", "builds"]:
            result = get_build(parts[2])
            if result is None:
                self.send_error_json(404, "Build not found")
                return
            build_dir, status = result

            if len(parts) == 3:
                self.send_json(200, status)
                return

            if len(parts) == 4 and parts[3] == "artifacts":
                self.send_json(200, {"artifacts": artifact_list(build_dir)})
                return

            if len(parts) == 4 and parts[3] == "events":
                if self.command == "HEAD":
                    self.send_bytes(405, b"", "text/plain; charset=utf-8")
                    return
                self.serve_build_events(build_dir, status)
                return

            if len(parts) in {5, 6} and parts[3] == "logs":
                job = parts[4]
                log_path = log_path_for(build_dir, status, job)
                if log_path is None:
                    self.send_error_json(404, "Log not found")
                    return
                if len(parts) == 5:
                    try:
                        snapshot = log_snapshot(log_path)
                    except OSError:
                        self.send_error_json(404, "Log not found")
                        return
                    snapshot["state"] = status.get("state") if job == "pipeline" else pipeline_jobs(status).get(job, {}).get("state")
                    self.send_json(200, snapshot)
                    return
                if parts[5] == "stream":
                    if self.command == "HEAD":
                        self.send_bytes(405, b"", "text/plain; charset=utf-8")
                        return
                    query = parse_qs(parsed.query)
                    try:
                        offset = int(query.get("offset", ["0"])[0])
                        if offset < 0:
                            raise ValueError
                    except ValueError:
                        self.send_error_json(400, "Invalid offset")
                        return
                    self.serve_log_stream(build_dir, status, job, offset)
                    return

            self.send_error_json(404, "Not found")
            return

        if path.startswith("/api/"):
            self.send_error_json(404, "Not found")
            return

        self.serve_static(path)

    def do_POST(self):
        self.send_error_json(405, "Kiln Web is read-only")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def main():
    if os.geteuid() == 0:
        print("kiln web: refusing to run as root", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer((HOST, PORT), KilnHandler)
    server.daemon_threads = True
    print(f"kiln web: listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
