#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'web' / 'server' / 'kiln_web.py'
spec = importlib.util.spec_from_file_location('kiln_web_http', MODULE_PATH)
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        builds = root / 'builds'
        static = root / 'static'
        build = builds / '20260826-demo-abc'
        (build / 'logs').mkdir(parents=True)
        static.mkdir()
        (static / 'index.html').write_text('<!doctype html><div id="root">SPA</div>', encoding='utf-8')
        (build / 'logs' / 'tests.log').write_text('hello\n', encoding='utf-8')
        (build / 'logs' / 'pipeline.log').write_text('pipeline\n', encoding='utf-8')
        status = {
            'build_id': build.name,
            'project': 'demo',
            'sha': 'a' * 40,
            'ref': 'refs/heads/main',
            'type': 'ci',
            'state': 'success',
            'pipeline': {
                'groups': {'quality': ['tests']},
                'jobs': {
                    'tests': {
                        'group': 'quality',
                        'needs': [],
                        'resolved_needs': [],
                        'state': 'success',
                        'log': 'logs/tests.log',
                    }
                },
            },
        }
        (build / 'status.json').write_text(json.dumps(status), encoding='utf-8')
        web.BUILDS = builds
        web.STATIC_ROOT = static
        web.SSE_POLL_SECONDS = 0.01

        server = ThreadingHTTPServer(('127.0.0.1', 0), web.KilnHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(base + '/api/builds', timeout=2) as response:
                body = json.load(response)
                assert body['builds'][0]['build_id'] == build.name

            with urllib.request.urlopen(base + f'/build/{build.name}', timeout=2) as response:
                assert 'SPA' in response.read().decode()
                assert response.headers.get_content_type() == 'text/html'

            with urllib.request.urlopen(base + f'/api/builds/{build.name}/logs/tests', timeout=2) as response:
                body = json.load(response)
                assert body['content'] == 'hello\n'
                assert body['offset'] == 6

            with urllib.request.urlopen(base + f'/api/builds/{build.name}/logs/tests/stream?offset=0', timeout=2) as response:
                stream = response.read().decode()
                assert 'event: chunk' in stream
                assert 'hello\\n' in stream
                assert 'event: end' in stream

            with urllib.request.urlopen(base + f'/api/builds/{build.name}/events', timeout=2) as response:
                events = response.read().decode()
                assert 'event: end' in events
                assert '"state":"success"' in events
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print('OK web http: JSON API, SPA fallback, and terminal SSE stream')


if __name__ == '__main__':
    main()
