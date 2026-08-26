#!/usr/bin/env python3
import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'web' / 'server' / 'kiln_web.py'

spec = importlib.util.spec_from_file_location('kiln_web', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_status(build_dir: Path, **overrides):
    status = {
        'build_id': build_dir.name,
        'project': 'demo',
        'sha': 'a' * 40,
        'ref': 'refs/heads/main',
        'type': 'ci',
        'state': 'running',
        'pipeline': {
            'groups': {'quality': ['tests']},
            'jobs': {
                'tests': {
                    'group': 'quality',
                    'needs': [],
                    'resolved_needs': [],
                    'state': 'running',
                    'log': 'logs/tests.log',
                }
            },
        },
    }
    status.update(overrides)
    (build_dir / 'status.json').write_text(json.dumps(status), encoding='utf-8')
    return status


def test_build_lookup_and_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build = root / '20260826-demo-abc'
        build.mkdir()
        write_status(build)
        module.BUILDS = root

        found = module.get_build(build.name)
        assert found is not None
        assert found[1]['project'] == 'demo'
        assert module.get_build('../etc') is None
        assert module.get_build('bad/name') is None


def test_list_builds_newest_first():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ('20260825-old', '20260826-new'):
            build = root / name
            build.mkdir()
            write_status(build)
        module.BUILDS = root
        builds = module.api_builds()
        assert [item['build_id'] for item in builds] == ['20260826-new', '20260825-old']


def test_log_snapshot_uses_raw_byte_offset_and_sanitizes():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'tests.log'
        raw = b'hello\n\x1b[31mred\x1b[0m\n'
        path.write_bytes(raw)
        snap = module.log_snapshot(path)
        assert snap['offset'] == len(raw)
        assert snap['content'] == 'hello\nred\n'
        assert snap['truncated'] is False


def test_read_appended_log_chunk():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'tests.log'
        path.write_bytes(b'old\nnew\n')
        chunk = module.read_log_chunk(path, 4)
        assert chunk == {'offset': 8, 'content': 'new\n'}


def test_artifacts_do_not_follow_symlinks():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp) / 'build'
        artifacts = build / 'artifacts' / 'job'
        artifacts.mkdir(parents=True)
        (artifacts / 'ok.txt').write_text('ok', encoding='utf-8')
        outside = Path(tmp) / 'outside.txt'
        outside.write_text('secret', encoding='utf-8')
        (artifacts / 'escape').symlink_to(outside)
        items = module.artifact_list(build)
        assert [item['path'] for item in items] == ['job/ok.txt']



def test_log_path_cannot_escape_logs_directory():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp) / 'build'
        (build / 'logs').mkdir(parents=True)
        outside = Path(tmp) / 'outside.log'
        outside.write_text('secret', encoding='utf-8')
        status = write_status(build)
        status['pipeline']['jobs']['tests']['log'] = '../outside.log'
        assert module.log_path_for(build, status, 'tests') is None

def test_diff_status_events_returns_only_deltas():
    previous = {
        'state': 'running',
        'pipeline': {'jobs': {'tests': {'state': 'running'}, 'build': {'state': 'pending'}}},
    }
    current = {
        'state': 'success',
        'duration_seconds': 4.2,
        'pipeline': {'jobs': {'tests': {'state': 'success', 'duration_seconds': 2.0}, 'build': {'state': 'success'}}},
    }
    events = module.diff_status_events(previous, current)
    assert ('job', {'name': 'tests', 'state': 'success', 'duration_seconds': 2.0}) in events
    assert ('job', {'name': 'build', 'state': 'success'}) in events
    assert ('build', {'state': 'success', 'duration_seconds': 4.2}) in events


def test_job_terminal():
    status = {'state': 'running', 'pipeline': {'jobs': {'tests': {'state': 'failed'}}}}
    assert module.job_terminal(status, 'tests') is True
    assert module.job_terminal(status, 'pipeline') is False
    status['state'] = 'failed'
    assert module.job_terminal(status, 'pipeline') is True


if __name__ == '__main__':
    tests = [name for name in globals() if name.startswith('test_')]
    for name in tests:
        globals()[name]()
        print(f'OK web api: {name}')
