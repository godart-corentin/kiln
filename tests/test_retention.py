#!/usr/bin/env python3
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'libexec'))
import kilnr_retention as retention
from kilnr_project_lock import project_locks, provision_project_locks

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        self.config = self.root / 'config'
        self.git = self.root / 'git'
        for path in ('state/builds', 'state/queue/incoming', 'state/queue/running', 'state/locks/projects', 'config', 'git'):
            (self.root / path).mkdir(parents=True, exist_ok=True)
        self.locks = self.state / 'locks/projects'
        self.locks.chmod(0o750)
        (self.state / 'locks/controller.lock').touch(mode=0o660)
        (self.state / 'locks/controller.lock').chmod(0o660)
        self.builds = self.state / 'builds'
        self.output = []
        self.sequence = 0
        self.project('demo')

    def project(self, name, policy=retention.DEFAULT_POLICY):
        repo = self.git / f'{name}.git'
        (repo / 'refs/kilnr/jobs').mkdir(parents=True, exist_ok=True)
        (repo / 'refs/heads').mkdir(exist_ok=True)
        (repo / 'refs/tags').mkdir(exist_ok=True)
        cfg = {'schema': 1, 'project': name, 'repository': str(repo)}
        if policy is not None:
            cfg['retention'] = policy
        (self.config / f'{name}.json').write_text(json.dumps(cfg))
        provision_project_locks(self.locks, [name])

    def build(self, age, project='demo', ref='refs/heads/main', kind='ci', state='success'):
        self.sequence += 1
        finished = NOW - timedelta(days=age)
        received = finished - timedelta(hours=1)
        sha = 'a' * 40
        name = f'{received:%Y%m%dT%H%M%S%fZ}-{project}-{sha[:7]}-{self.sequence:08x}'
        path = self.builds / name
        path.mkdir()
        job = {'schema': 1, 'id': name, 'project': project, 'received_at': received.isoformat(),
               'old_sha': sha, 'new_sha': sha, 'sha': sha, 'ref': ref, 'type': kind,
               'pin_ref': f'refs/kilnr/jobs/{name}'}
        status = dict(job, build_id=name, job_id=name, state=state, finished_at=finished.isoformat())
        (path / 'job.json').write_text(json.dumps(job))
        (path / 'status.json').write_text(json.dumps(status))
        (path / 'work').mkdir()
        (path / 'work/data').write_text('payload')
        return path

    def run_cleanup(self, **kwargs):
        self.output.clear()
        return retention.cleanup(state=self.state, config=self.config, git=self.git, now=NOW, report=self.output.append, **kwargs)

    def mutate(self, build, filename, **changes):
        path = build / filename
        value = json.loads(path.read_text())
        value.update(changes)
        path.write_text(json.dumps(value))

    def test_age_and_exact_boundary(self):
        new, boundary, old = self.build(1), self.build(30), self.build(31)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(new.exists())
        self.assertTrue(boundary.exists())
        self.assertFalse(old.exists())
        self.assertIn('max age', '\n'.join(self.output))

    def test_count_per_project_and_ref(self):
        limits = dict(retention.DISABLED_POLICY, max_builds_per_ref=2)
        self.project('demo', limits)
        self.project('other', limits)
        groups = []
        for project, ref in [('demo', 'refs/heads/main'), ('demo', 'refs/heads/feature/x'), ('other', 'refs/heads/main')]:
            groups.append([self.build(age, project, ref) for age in [1, 2, 3, 4]])
        self.assertEqual(self.run_cleanup(), 0)
        for group in groups:
            self.assertEqual([p.exists() for p in group], [True, True, False, False])

    def test_completion_order_and_tie_breaker(self):
        self.project('demo', dict(retention.DISABLED_POLICY, max_builds_per_ref=1))
        a, b = self.build(2), self.build(1)
        # Receipt order remains older, completion order is newer.
        self.mutate(a, 'status.json', finished_at=NOW.isoformat())
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        c = self.build(0)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertFalse(a.exists())
        self.assertTrue(c.exists())

    def test_age_and_count_are_union(self):
        self.project('demo', dict(retention.DEFAULT_POLICY, max_builds_per_ref=1))
        old, older = self.build(40), self.build(50)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertFalse(old.exists())
        self.assertFalse(older.exists())
        self.assertIn('max age, excess builds for ref', '\n'.join(self.output))

    def test_releases_preserved_and_explicit_opt_out(self):
        release = self.build(100, kind='release', ref='refs/tags/v1.0.0')
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(release.exists())
        self.project('demo', dict(retention.DEFAULT_POLICY, keep_releases=False))
        self.assertEqual(self.run_cleanup(), 0)
        self.assertFalse(release.exists())

    def test_nonterminal_and_active_queues(self):
        paths = [self.build(50, state=state) for state in ['running', 'preparing', 'queued']]
        for queue in ['incoming', 'running']:
            path = self.build(50)
            paths.append(path)
            shutil.copyfile(path / 'job.json', self.state / 'queue' / queue / (path.name + '.json'))
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(all(p.exists() for p in paths))

    def test_corrupt_queue_fails_closed(self):
        path = self.build(50)
        (self.state / 'queue/incoming/bad.json').write_text('[]')
        self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue(path.exists())

    def test_terminal_failures_are_eligible(self):
        paths = [self.build(50, state=state) for state in ['failed', 'aborted']]
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_metadata_identity_failures(self):
        variants = [('job.json', {'project': 'other'}), ('status.json', {'project': 'other'}),
                    ('job.json', {'pin_ref': 'refs/heads/main'}), ('job.json', {'id': '../victim'}),
                    ('job.json', {'sha': 'b' * 40}), ('status.json', {'sha': 'b' * 40}),
                    ('status.json', {'finished_at': None}), ('status.json', {'finished_at': '2020-01-01'}),
                    ('status.json', {'job_id': '../victim'}), ('job.json', {'ref': 'refs/heads/../victim'})]
        paths = []
        for filename, changes in variants:
            path = self.build(50)
            self.mutate(path, filename, **changes)
            paths.append(path)
        victim = self.root / 'victim'
        victim.write_text('keep')
        self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertEqual(victim.read_text(), 'keep')

    def test_symlink_build_and_metadata_are_refused(self):
        path = self.build(50)
        target = self.root / 'outside'
        path.rename(target)
        path.symlink_to(target, target_is_directory=True)
        other = self.build(50)
        outside_status = self.root / 'status'
        (other / 'status.json').rename(outside_status)
        (other / 'status.json').symlink_to(outside_status)
        self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue((target / 'work/data').exists())
        self.assertTrue(other.exists())

    def test_payload_symlinks_are_unlinked_not_followed(self):
        path = self.build(50)
        target = self.root / 'outside'
        target.mkdir()
        (target / 'precious').write_text('keep')
        (path / 'work/link').symlink_to(target, target_is_directory=True)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertFalse(path.exists())
        self.assertEqual((target / 'precious').read_text(), 'keep')

    def test_symlink_roots_and_project_traversal(self):
        path = self.build(50)
        alias = self.root / 'alias'
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(OSError):
            retention.cleanup(state=alias, config=self.config, git=self.git)
        with self.assertRaises(ValueError):
            self.run_cleanup(project='../demo')
        self.assertTrue(path.exists())

    def test_hardlinked_metadata_refused(self):
        path = self.build(50)
        os.link(path / 'job.json', self.root / 'job-link')
        self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue(path.exists())

    def test_metadata_nonowner_writers_refused(self):
        path = self.build(50)
        (path / 'status.json').chmod(0o666)
        self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue(path.exists())

    def test_dry_run_same_candidates_and_idempotence(self):
        paths = [self.build(age) for age in [1, 40, 50]]
        pins = self.git / 'demo.git/refs/kilnr/jobs'
        (pins / paths[1].name).write_text('a' * 40 + '\n')
        self.assertEqual(self.run_cleanup(dry_run=True), 0)
        dry = [line.replace('Would delete', 'Deleting') for line in self.output]
        self.assertTrue(all(p.exists() for p in paths))
        self.assertTrue((pins / paths[1].name).exists())
        self.assertFalse(any(p.name.startswith(retention.PREFIX) for p in self.builds.iterdir()))
        self.assertEqual(self.run_cleanup(), 0)
        self.assertEqual(dry, self.output)
        self.assertFalse((pins / paths[1].name).exists())
        self.assertEqual(self.run_cleanup(), 0)
        self.assertEqual(self.output, [])

    def test_pin_mismatch_and_symlink_and_lock_retained(self):
        pins = self.git / 'demo.git/refs/kilnr/jobs'
        a, b, c = self.build(50), self.build(50), self.build(50)
        (pins / a.name).write_text('b' * 40 + '\n')
        (pins / b.name).symlink_to(pins / a.name)
        (pins / (c.name + '.lock')).touch()
        self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue(all(path.exists() for path in [a, b, c]))

    def test_packed_pin_fails_closed_and_ordinary_refs_untouched(self):
        path = self.build(50)
        repo = self.git / 'demo.git'
        ordinary = repo / 'refs/heads/main'
        ordinary.write_text('a' * 40 + '\n')
        packed = repo / 'packed-refs'
        packed.write_text(f"{'a' * 40} refs/kilnr/jobs/{path.name}\n")
        self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue(path.exists())
        self.assertIn('administrator repair', '\n'.join(self.output))
        packed.unlink()
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(ordinary.exists())

    def test_existing_config_and_policy_validation(self):
        self.project('demo', None)
        path = self.build(100)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(path.exists())
        for value in [None, [], {'max_age_days': 0}, {'max_age_days': True}, {'max_builds_per_ref': -1}, {'keep_releases': 'yes'}, {'max_age_day': 30}]:
            with self.assertRaises(ValueError):
                retention.policy({'retention': value})
        self.assertEqual(retention.policy({'retention': {}}), retention.DISABLED_POLICY)

    def test_project_scope_and_malformed_config(self):
        self.project('other')
        a, b = self.build(50), self.build(50, 'other')
        self.assertEqual(self.run_cleanup(project='demo'), 0)
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())
        cfg = json.loads((self.config / 'other.json').read_text())
        cfg['repository'] = str(self.git / 'demo.git')
        (self.config / 'other.json').write_text(json.dumps(cfg))
        self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue(b.exists())

    def test_controller_and_project_locks(self):
        path = self.build(50)
        with (self.state / 'locks/controller.lock').open('r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.assertEqual(self.run_cleanup(), 0)
            self.assertTrue(path.exists())
            self.assertIn('controller is active', self.output[0])
        with project_locks(self.locks, ['demo'], exclusive=False):
            self.assertEqual(self.run_cleanup(), 0)
            self.assertTrue(path.exists())
            self.assertIn('project demo is busy', self.output[0])
        self.assertEqual(self.run_cleanup(), 0)
        self.assertFalse(path.exists())

    def test_status_lock_protects_terminal_build(self):
        path = self.build(50)
        lock = path / 'status.lock'
        lock.touch(mode=0o640)
        with lock.open('r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.assertEqual(self.run_cleanup(), 1)
            self.assertTrue(path.exists())
        self.assertEqual(self.run_cleanup(), 0)
        self.assertFalse(path.exists())

    def test_invalid_directory_names_remain_untouched(self):
        for name in ['demo', '..demo', '20260901-demo-abcdefg', '20260901T000000000000Z-other-aaaaaaa-00000001']:
            (self.builds / name).mkdir()
        self.assertEqual(self.run_cleanup(project='demo'), 0)
        self.assertEqual(len(list(self.builds.iterdir())), 4)

    def test_pin_repository_symlink_cannot_cross_project(self):
        self.project('other')
        path = self.build(50)
        original = self.git / 'demo.git'
        shutil.rmtree(original)
        original.symlink_to(self.git / 'other.git', target_is_directory=True)
        other_pin = self.git / 'other.git/refs/kilnr/jobs' / path.name
        other_pin.write_text('a' * 40)
        self.assertEqual(self.run_cleanup(project='demo'), 1)
        self.assertTrue(path.exists())
        self.assertTrue(other_pin.exists())

    def test_real_git_pin_removal_preserves_branches_tags_and_other_pins(self):
        repo = self.git / 'demo.git'
        def git(*args, **kwargs):
            return subprocess.run(['git', f'--git-dir={repo}', *args], text=True, capture_output=True, check=True, **kwargs).stdout.strip()
        git('init', '--bare')
        git('config', 'gc.packRefs', 'false')
        blob = git('hash-object', '-w', '--stdin', input='test')
        tree = git('mktree', input=f'100644 blob {blob}\tfile\n')
        env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.test', GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.test')
        sha = git('commit-tree', tree, input='test\n', env=env)
        path = self.build(50)
        name = path.name.replace('aaaaaaa', sha[:7])
        new_path = path.with_name(name)
        path.rename(new_path)
        path = new_path
        self.mutate(path, 'job.json', id=name, old_sha=sha, new_sha=sha, sha=sha, pin_ref=f'refs/kilnr/jobs/{name}')
        self.mutate(path, 'status.json', build_id=name, job_id=name, sha=sha)
        for ref in [f'refs/kilnr/jobs/{name}', 'refs/kilnr/jobs/unrelated', 'refs/heads/main', 'refs/tags/v1.0.0']:
            git('update-ref', ref, sha)
        self.assertEqual(self.run_cleanup(), 0)
        refs = git('for-each-ref', '--format=%(refname)').splitlines()
        self.assertEqual(refs, ['refs/heads/main', 'refs/kilnr/jobs/unrelated', 'refs/tags/v1.0.0'])

    def test_concurrent_disappearance(self):
        path = self.build(50)
        original = retention.candidates
        def disappear(*args):
            selected = original(*args)
            shutil.rmtree(path)
            return selected
        with patch.object(retention, 'candidates', disappear):
            self.assertEqual(self.run_cleanup(), 0)

    def test_interrupted_deletion_resumes_even_if_policy_disabled(self):
        path = self.build(50)
        original = retention.remove_tree
        def interrupt(parent, name, device):
            if name == 'build':
                raise OSError('simulated interruption')
            return original(parent, name, device)
        with patch.object(retention, 'remove_tree', interrupt):
            self.assertEqual(self.run_cleanup(), 1)
        self.assertFalse(path.exists())
        pending = self.builds / (retention.PREFIX + path.name)
        self.assertTrue((pending / 'record.json').exists())
        self.project('demo', None)
        self.assertEqual(self.run_cleanup(dry_run=True), 0)
        self.assertTrue(pending.exists())
        self.assertEqual(self.run_cleanup(), 0)
        self.assertFalse(pending.exists())

    def test_recovery_survives_partial_payload_metadata_removal(self):
        path = self.build(50)
        original = retention.remove_tree
        def interrupt(parent, name, device):
            if name == 'build':
                with retention.directory(name, parent) as fd:
                    os.unlink('job.json', dir_fd=fd)
                    os.unlink('status.json', dir_fd=fd)
                raise OSError('interrupted after deleting metadata')
            return original(parent, name, device)
        with patch.object(retention, 'remove_tree', interrupt):
            self.assertEqual(self.run_cleanup(), 1)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertEqual(list(self.builds.iterdir()), [])

    def test_interrupted_before_rename_preserves_original(self):
        path = self.build(50)
        with patch.object(retention.os, 'rename', side_effect=OSError('interrupted')):
            self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue(path.exists())
        self.project('demo', None)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(path.exists())
        self.assertEqual(list(self.builds.iterdir()), [path])

    def test_transaction_cannot_cross_project_or_escape(self):
        path = self.build(50)
        pending = self.builds / (retention.PREFIX + path.name)
        pending.mkdir()
        job = json.loads((path / 'job.json').read_text())
        status = json.loads((path / 'status.json').read_text())
        job['project'] = 'other'
        (pending / 'record.json').write_text(json.dumps({'job': job, 'status': status}))
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').touch()
        (pending / 'build').symlink_to(outside, target_is_directory=True)
        self.project('demo', None)
        self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue((outside / 'keep').exists())

    def test_bind_mounts_on_same_device_are_refused(self):
        path = self.build(50)
        line = f'42 1 8:1 /outside {path}/work rw - ext4 /dev/sda rw\n'
        with self.assertRaises(ValueError):
            retention.reject_nested_mounts(self.builds, line)
        # A filesystem mounted at the builds root itself is supported.
        retention.reject_nested_mounts(self.builds, f'42 1 8:1 / {self.builds} rw - ext4 /dev/sda rw\n')

    def test_mount_boundary_refused(self):
        path = self.build(50)
        original = retention.tree_check
        def different_device(fd, device):
            return original(fd, device + 1)
        with patch.object(retention, 'tree_check', different_device):
            self.assertEqual(self.run_cleanup(), 1)
        self.assertTrue(path.exists())

    def test_rerun_holds_project_lock_until_enqueue(self):
        loader = importlib.machinery.SourceFileLoader('rerun_test', str(ROOT / 'libexec/rerun'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        rerun = importlib.util.module_from_spec(spec)
        loader.exec_module(rerun)
        path = self.build(50)
        actual_uid = os.geteuid()
        def enqueue(*args, **kwargs):
            with patch.object(retention.os, "geteuid", return_value=actual_uid):
                self.assertEqual(self.run_cleanup(), 0)
            self.assertTrue(path.exists())
            self.assertIn('project demo is busy', self.output[0])
            return subprocess.CompletedProcess(args, 0, 'new-job\n', '')
        with patch.object(rerun, 'BUILDS', self.builds), patch.object(rerun, 'PROJECT_LOCK_ROOT', self.locks), patch.object(rerun.os, 'geteuid', return_value=0), patch.object(rerun.subprocess, 'run', enqueue), patch.object(sys, 'argv', ['rerun', path.name]):
            self.assertEqual(rerun.main(), 0)


class InstallationTests(unittest.TestCase):
    def test_unit_and_defaults_installation_twice(self):
        script = (ROOT / 'install.sh').read_text()
        start = script.index('if [[ ! -f /etc/kilnr/defaults.json ]]')
        defaults = script[start:script.index('\nfi', start) + 3]
        start = script.index('for unit in ')
        units = script[start:script.index('\necho', start)]
        self.assertIn('kilnr-cleanup.service', units)
        self.assertIn('systemctl enable --now kilnr-cleanup.timer', units)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'etc/kilnr').mkdir(parents=True)
            (base / 'etc/systemd/system').mkdir(parents=True)
            (base / 'etc/kilnr/defaults.json').write_text('{"existing":true}\n')
            body = defaults + '\n' + units
            body = body.replace('/etc/', str(base / 'etc') + '/').replace('install -o root -g root', 'install')
            body = 'set -eu\nsystemctl() { :; }\n' + body
            for _ in range(2):
                subprocess.run(['bash', '-c', body], env=dict(os.environ, ROOT_DIR=str(ROOT)), check=True)
            self.assertEqual((base / 'etc/kilnr/defaults.json').read_text(), '{"existing":true}\n')
            for unit in ('kilnr-cleanup.service', 'kilnr-cleanup.timer'):
                self.assertEqual((base / 'etc/systemd/system' / unit).read_bytes(), (ROOT / 'systemd' / unit).read_bytes())
        service = (ROOT / 'systemd/kilnr-cleanup.service').read_text()
        self.assertIn('User=kilnr', service)
        self.assertIn('ProtectSystem=strict', service)
        self.assertNotIn('SupplementaryGroups=docker', service)
        self.assertIn('"$ROOT_DIR/install.sh" --update', (ROOT / 'update.sh').read_text())
        self.assertIn('kilnr-cleanup.timer', (ROOT / 'uninstall.sh').read_text())

    def test_project_creation_copies_policy_without_upgrade_inheritance(self):
        script = (ROOT / 'libexec/project-create').read_text()
        code = script.split("<<'PY'\n", 1)[1].split('\nPY', 1)[0]
        code = code.replace('/usr/local/libexec/kilnr', str(ROOT / 'libexec'))
        with tempfile.TemporaryDirectory() as tmp:
            defaults = Path(tmp) / 'defaults.json'
            for include in (False, True):
                value = {'runner': {}}
                if include:
                    value['retention'] = retention.DEFAULT_POLICY
                defaults.write_text(json.dumps(value))
                result = subprocess.run([sys.executable, '-c', code, 'demo', '/srv/git/demo.git', '/etc/kilnr/secrets/demo.discord-webhook', str(defaults)], text=True, capture_output=True, check=True)
                cfg = json.loads(result.stdout)
                self.assertEqual('retention' in cfg, include)
                self.assertEqual(retention.policy(cfg), retention.DEFAULT_POLICY if include else retention.DISABLED_POLICY)


if __name__ == '__main__':
    unittest.main()
