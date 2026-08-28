#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "web" / "frontend"


def fail(message):
    print(f"FAIL frontend: {message}", file=sys.stderr)
    raise SystemExit(1)


package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
for dependency in ("react", "react-dom", "@tanstack/react-router"):
    if dependency not in package.get("dependencies", {}):
        fail(f"missing dependency {dependency}")
for dependency in ("vite", "typescript", "@vitejs/plugin-react"):
    if dependency not in package.get("devDependencies", {}):
        fail(f"missing dev dependency {dependency}")

router = (FRONTEND / "src" / "router.tsx").read_text(encoding="utf-8")
for route in ('path: \'/\'', "'/build/$buildId'", "'/build/$buildId/logs/$job'"):
    if route not in router:
        fail(f"missing route {route}")

log_viewer = (FRONTEND / "src" / "components" / "LogViewer.tsx").read_text(encoding="utf-8")
if "EventSource" not in log_viewer or "offsetRef" not in log_viewer:
    fail("LogViewer must append through offset-aware EventSource")
if "window.location.reload" in log_viewer or "meta http-equiv" in log_viewer:
    fail("LogViewer must not refresh the page")

build_page = (FRONTEND / "src" / "routes" / "BuildPage.tsx").read_text(encoding="utf-8")
if "PipelineGraph" not in build_page or "EventSource" not in build_page:
    fail("build page must render the DAG and subscribe to live state")

graph = (FRONTEND / "src" / "components" / "PipelineGraph.tsx").read_text(encoding="utf-8")
if "resolved_needs" not in graph:
    fail("PipelineGraph must use controller-resolved dependencies")
if ".needs" in graph:
    fail("PipelineGraph must not resolve raw needs")

server = (ROOT / "web" / "server" / "kilnr_web.py").read_text(encoding="utf-8")
if "text/event-stream" not in server:
    fail("web backend must expose SSE")
if "STATIC_ROOT" not in server:
    fail("web backend must serve the built SPA")


dockerfile = (ROOT / "web" / "Dockerfile").read_text(encoding="utf-8")
if "FROM node:22-alpine AS frontend" not in dockerfile:
    fail("frontend must build in a Node stage")
if "FROM python:3.12-alpine" not in dockerfile:
    fail("runtime image must be Python-only")
if "COPY --from=frontend /src/dist /opt/kilnr/static" not in dockerfile:
    fail("runtime image must contain the Vite build")

install = (ROOT / "install.sh").read_text(encoding="utf-8")
if "/usr/local/share/kilnr/web-src" not in install:
    fail("install.sh must stage web build sources for update.sh")


update = (ROOT / "update.sh").read_text(encoding="utf-8")
if "up -d --build kilnr-web" not in update:
    fail("update.sh must rebuild an already-migrated React web image")
if "docker restart kilnr-web" in update:
    fail("update.sh must not restart the legacy web container before React migration")

print("OK frontend: React routes, DAG, and live logs are wired")
