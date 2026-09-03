"""Completed-build retention. All mutation paths are descriptor-relative.

Callers supply roots only in tests; the administrator entry point has fixed roots.
Lock order is controller, then project (nonblocking). No repository payload paths
are used as deletion targets.
"""
import fcntl
import json
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from kilnr_project_lock import (
    ProjectLockBusy, _open_directory_chain, _validate_lock_entry, project_locks, validate_project_name,
)

STATE = Path('/var/lib/kilnr')
CONFIG = Path('/etc/kilnr/projects')
GIT = Path('/srv/git')
DEFAULT_POLICY = {'max_age_days': 30, 'max_builds_per_ref': 10, 'keep_releases': True}
DISABLED_POLICY = {'max_age_days': None, 'max_builds_per_ref': None, 'keep_releases': True}
BUILD_RE = re.compile(r'^(\d{8}T\d{12}Z)-([a-z0-9][a-z0-9_-]{0,62})-([0-9a-f]{7})-([0-9a-f]{8})$')
OID_RE = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
PREFIX = '.cleanup-'
MAX_JSON = 4 * 1024 * 1024


def policy(config):
    if 'retention' not in config:
        return dict(DISABLED_POLICY)
    value = config['retention']
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError('invalid retention object or unknown retention field')
    # Omitted limits are disabled, never an implicit destructive default.
    result = dict(DISABLED_POLICY, **value)
    for key in ('max_age_days', 'max_builds_per_ref'):
        number = result[key]
        if number is not None and (type(number) is not int or not 1 <= number <= 1000000):
            raise ValueError(f'retention.{key} must be a positive integer or null')
    if type(result['keep_releases']) is not bool:
        raise ValueError('retention.keep_releases must be boolean')
    return result


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamp must include timezone')
    return parsed.astimezone(timezone.utc)


@contextmanager
def directory(name, parent=None):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def read_bytes(parent, name, limit=MAX_JSON):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError(f'unsafe metadata: {name}')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError(f'metadata too large: {name}')
        return value
    finally:
        os.close(fd)


def read_json(parent, name):
    if name in ('job.json', 'status.json', 'record.json'):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if info.st_uid != os.fstat(parent).st_uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError(f'unsafe metadata ownership/mode: {name}')
    value = json.loads(read_bytes(parent, name))
    if not isinstance(value, dict):
        raise ValueError(f'metadata must be an object: {name}')
    return value


def validate_build(build_id, job, status, project):
    if not isinstance(job, dict) or not isinstance(status, dict):
        raise ValueError("invalid build metadata object")
    match = BUILD_RE.fullmatch(build_id)
    if not match or match[2] != validate_project_name(project):
        raise ValueError('invalid structured build id/project')
    if job.get('schema') != 1 or status.get('schema') != 1:
        raise ValueError('invalid metadata schema')
    if job.get('id') != build_id or job.get('project') != project:
        raise ValueError('job identity mismatch')
    for key in ('old_sha', 'new_sha', 'sha'):
        if not isinstance(job.get(key), str) or not OID_RE.fullmatch(job[key]):
            raise ValueError(f'invalid {key}')
    if match[1] != timestamp(job.get('received_at')).strftime('%Y%m%dT%H%M%S%fZ') or match[3] != job['sha'][:7]:
        raise ValueError('build id does not match timestamp/SHA')
    if job.get('pin_ref') != f'refs/kilnr/jobs/{build_id}':
        raise ValueError('invalid job pin')
    kind, ref = job.get('type'), job.get('ref')
    prefix = {'ci': 'refs/heads/', 'release': 'refs/tags/'}.get(kind)
    if not prefix or not isinstance(ref, str) or not ref.startswith(prefix) or len(ref) <= len(prefix):
        raise ValueError('invalid build type/ref')
    if any(ord(char) < 32 or ord(char) == 127 for char in ref) or '..' in ref or '\\' in ref:
        raise ValueError('unsafe ref')
    for key in ('project', 'sha', 'ref', 'type', 'received_at'):
        if status.get(key) != job.get(key):
            raise ValueError(f'status/job {key} mismatch')
    if status.get('build_id') != build_id or status.get('job_id') != build_id:
        raise ValueError('status identity mismatch')
    if status.get('state') not in ('success', 'failed', 'aborted'):
        return None
    finished = timestamp(status.get('finished_at'))
    if finished < timestamp(job['received_at']):
        raise ValueError('completion precedes receipt')
    return finished


def candidates(builds, retention, now):
    groups = {}
    for item in builds:
        if item['job']['type'] == 'release' and retention['keep_releases']:
            continue
        groups.setdefault((item['job']['project'], item['job']['ref']), []).append(item)
    selected = []
    for group in groups.values():
        group.sort(key=lambda item: (item['finished'], item['id']), reverse=True)
        for index, item in enumerate(group):
            reasons = []
            age = (now - item['finished']).total_seconds() / 86400
            if retention['max_age_days'] is not None and age > retention['max_age_days']:
                reasons.append('max age')
            if retention['max_builds_per_ref'] is not None and index >= retention['max_builds_per_ref']:
                reasons.append('excess builds for ref')
            if reasons:
                selected.append(dict(item, reasons=reasons, age=age))
    return sorted(selected, key=lambda item: (item['finished'], item['id']))


def reject_nested_mounts(builds, mountinfo=None):
    # st_dev alone misses Linux bind mounts on the same filesystem. Builds may
    # occupy their own filesystem, but no descendant may be a mount point.
    if mountinfo is None:
        if not sys.platform.startswith('linux'):
            return
        mountinfo = Path('/proc/self/mountinfo').read_text(encoding='utf-8')
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 6:
            raise ValueError('invalid mount table')
        mount = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4])
        try:
            relative = Path(mount).relative_to(builds)
        except ValueError:
            continue
        if relative.parts:
            raise ValueError(f'refusing nested mount beneath builds: {mount}')


def tree_check(fd, device):
    """Refuse mount boundaries before mutation; payload symlinks are only unlinked."""
    if os.fstat(fd).st_dev != device:
        raise ValueError('refusing filesystem boundary')
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_dev != device:
            raise ValueError('refusing filesystem boundary')
        if stat.S_ISDIR(info.st_mode):
            with directory(name, fd) as child:
                tree_check(child, device)


@contextmanager
def status_lock(build_fd):
    # Preparation failures may never have acquired a status lock.
    try:
        fd = os.open('status.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=build_fd)
    except FileNotFoundError:
        yield
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('unsafe status lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def remove_tree(parent, name, device):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if info.st_dev != device:
            raise ValueError('refusing filesystem boundary')
        if stat.S_ISDIR(info.st_mode):
            with directory(name, parent) as child:
                if os.fstat(child).st_dev != device:
                    raise ValueError('refusing filesystem boundary')
                for entry in os.listdir(child):
                    remove_tree(child, entry, device)
            os.rmdir(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass


def pin_cleanup(git_fd, project, item, dry_run):
    """Pins are preparation-scoped. Retry leftover loose pins before retiring a build.

    gc.packRefs=false is the installed policy. Packed pins cannot be removed by
    the kilnr identity without broadening repository permissions: fail closed.
    """
    with directory(f'{project}.git', git_fd) as repo:
        try:
            packed = read_bytes(repo, 'packed-refs', 64 * 1024 * 1024).decode('ascii')
        except FileNotFoundError:
            packed = ''
        ref = f"refs/kilnr/jobs/{item['id']}"
        if any(line.split()[-1:] == [ref] for line in packed.splitlines()):
            raise ValueError(f'packed job pin needs administrator repair: {ref}')
        with directory('refs', repo) as refs, directory('kilnr', refs) as namespace, directory('jobs', namespace) as jobs:
            lock_name = item['id'] + '.lock'
            if dry_run:
                try:
                    os.stat(lock_name, dir_fd=jobs, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError('job pin is locked')
                lock_fd = None
            else:
                lock_fd = os.open(lock_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=jobs)
            try:
                try:
                    value = read_bytes(jobs, item['id'], 128).decode('ascii').strip()
                except FileNotFoundError:
                    return
                if value != item['job']['sha']:
                    raise ValueError('job pin SHA mismatch')
                if not dry_run:
                    os.unlink(item['id'], dir_fd=jobs)
                    os.fsync(jobs)
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
                    os.unlink(lock_name, dir_fd=jobs)
                    os.fsync(jobs)


def active_ids(state_fd):
    result = set()
    with directory('queue', state_fd) as queue:
        for name in ('incoming', 'running'):
            with directory(name, queue) as fd:
                # Even malformed or symlinked queue entries protect their filename.
                result.update(entry[:-5] for entry in os.listdir(fd) if entry.endswith('.json'))
                for entry in os.listdir(fd):
                    if not entry.endswith('.json'):
                        continue
                    try:
                        job = read_json(fd, entry)
                    except FileNotFoundError:
                        continue
                    # Fail closed on corrupt queue data, not a guessed project identity.
                    if not isinstance(job.get('id'), str):
                        raise ValueError('invalid active queue identity')
                    result.add(job['id'])
    return result


def retire(builds_fd, item):
    name = PREFIX + item['id']
    os.mkdir(name, 0o750, dir_fd=builds_fd)
    with directory(name, builds_fd) as txn:
        fd = os.open('record.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=txn)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'job': item['job'], 'status': item['status']}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(txn)
        os.fsync(builds_fd)
        os.rename(item['id'], 'build', src_dir_fd=builds_fd, dst_dir_fd=txn)
        os.fsync(txn)
        os.fsync(builds_fd)
        finish_transaction(builds_fd, name, txn)


def finish_transaction(builds_fd, name, txn):
    remove_tree(txn, 'build', os.fstat(builds_fd).st_dev)
    os.fsync(txn)
    os.unlink('record.json', dir_fd=txn)
    os.fsync(txn)
    os.rmdir(name, dir_fd=builds_fd)
    os.fsync(builds_fd)


def cleanup(*, state=STATE, config=CONFIG, git=GIT, project=None, dry_run=False, now=None, report=print):
    if project is not None:
        validate_project_name(project)
    now = now or datetime.now(timezone.utc)
    errors = 0
    with ExitStack() as stack:
        # Reject symlinks in every ancestor, including the fixed roots.
        state_fd = _open_directory_chain(stack, state, search_only=True)[1][-1][1]
        config_fd = _open_directory_chain(stack, config)[1][-1][1]
        git_fd = _open_directory_chain(stack, git)[1][-1][1]
        locks = stack.enter_context(directory('locks', state_fd))
        lock = os.open('controller.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=locks)
        stack.callback(os.close, lock)
        _validate_lock_entry(lock, os.fstat(locks), 'controller.lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report('Deferred: controller is active')
            return 0
        builds_fd = stack.enter_context(directory('builds', state_fd))
        info = os.fstat(builds_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('unsafe builds root ownership/mode')
        reject_nested_mounts(state / 'builds')
        projects = [project] if project else sorted(name[:-5] for name in os.listdir(config_fd) if name.endswith('.json'))
        for current in projects:
            try:
                validate_project_name(current)
                with project_locks(state / 'locks/projects', [current], exclusive=True, blocking=False):
                    cfg = read_json(config_fd, f'{current}.json')
                    if cfg.get('schema') != 1 or cfg.get('project') != current or cfg.get('repository') != str(git / f'{current}.git'):
                        raise ValueError('project configuration identity mismatch')
                    retention = policy(cfg)
                    active = active_ids(state_fd)
                    items = []
                    for name in sorted(os.listdir(builds_fd)):
                        pending = name.startswith(PREFIX)
                        build_id = name[len(PREFIX):] if pending else name
                        match = BUILD_RE.fullmatch(build_id)
                        if not match or match[2] != current:
                            continue
                        if build_id in active:
                            continue
                        try:
                            with directory(name, builds_fd) as fd:
                                info = os.fstat(fd)
                                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                                    raise ValueError('unsafe build directory ownership/mode')
                                if pending:
                                    entries = set(os.listdir(fd))
                                    if not entries:
                                        if not dry_run:
                                            os.rmdir(name, dir_fd=builds_fd)
                                        continue
                                    if entries - {'record.json', 'build'}:
                                        raise ValueError('unexpected cleanup transaction entries')
                                    if 'build' not in entries:
                                        # Interrupted before publication, or after payload deletion.
                                        # Only an ordinary record may remain; no payload is inferred.
                                        info = os.stat('record.json', dir_fd=fd, follow_symlinks=False)
                                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                            raise ValueError('unsafe cleanup record')
                                        report(f"{'Would finish' if dry_run else 'Finishing'} empty cleanup transaction: {build_id}")
                                        if not dry_run:
                                            finish_transaction(builds_fd, name, fd)
                                        continue
                                    with directory('build', fd):
                                        pass
                                    record = read_json(fd, 'record.json')
                                    job, status = record['job'], record['status']
                                else:
                                    job, status = read_json(fd, 'job.json'), read_json(fd, 'status.json')
                                finished = validate_build(build_id, job, status, current)
                                if finished is None:
                                    continue
                                item = {'id': build_id, 'job': job, 'status': status, 'finished': finished}
                                if pending:
                                    tree_check(fd, os.fstat(builds_fd).st_dev)
                                    report(f"{'Would finish' if dry_run else 'Finishing'} interrupted cleanup: {build_id}")
                                    if not dry_run:
                                        finish_transaction(builds_fd, name, fd)
                                else:
                                    items.append(item)
                        except FileNotFoundError:
                            continue
                        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                            errors += 1
                            report(f'Refused {name}: {exc}')
                    for item in candidates(items, retention, now):
                        try:
                            with directory(item['id'], builds_fd) as fd, status_lock(fd):
                                # Revalidate immediately before pin mutation and rename.
                                if read_json(fd, 'job.json') != item['job'] or read_json(fd, 'status.json') != item['status']:
                                    raise ValueError('metadata changed during cleanup')
                                tree_check(fd, os.fstat(builds_fd).st_dev)
                                try:
                                    pin_cleanup(git_fd, current, item, dry_run)
                                except OSError as exc:
                                    raise ValueError(f'cannot safely clean job pin: {exc}') from exc
                                report(f"{'Would delete' if dry_run else 'Deleting'} {item['id']} project={current} ref={item['job']['ref']} age={item['age']:.2f}d reason={', '.join(item['reasons'])}")
                                if not dry_run:
                                    retire(builds_fd, item)
                        except FileNotFoundError:
                            continue
                        except (OSError, ValueError, RecursionError) as exc:
                            errors += 1
                            report(f"Refused {item['id']}: {exc}")
            except ProjectLockBusy:
                report(f'Deferred: project {current} is busy')
            except (OSError, ValueError) as exc:
                errors += 1
                report(f'Refused project {current}: {exc}')
    return 1 if errors else 0
