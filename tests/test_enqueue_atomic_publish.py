#!/usr/bin/env python3

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

enqueue = (ROOT / "libexec" / "enqueue").read_text()
install = (ROOT / "install.sh").read_text()
update = (ROOT / "update.sh").read_text()


# enqueue must verify that incoming can be opened before making
# the job visible, then fsync the directory after atomic rename.
open_pos = enqueue.index("dir_fd = os.open(")
replace_pos = enqueue.index("os.replace(tmp_path, final_path)")
fsync_pos = enqueue.index("os.fsync(dir_fd)")

assert open_pos < replace_pos < fsync_pos, (
    "enqueue must open incoming before publishing the job, "
    "then fsync it after os.replace()"
)


# git needs read/write/traverse on incoming because enqueue opens
# the directory O_RDONLY before fsyncing it.
acl = "setfacl -m u:git:rwx /var/lib/kilnr/queue/incoming"

assert acl in install, (
    "install.sh must grant git rwx on incoming so directory fsync works"
)


# update.sh intentionally delegates migrations to install.sh --update.
assert '"$ROOT_DIR/install.sh" --update' in update, (
    "update.sh must delegate updates to install.sh --update"
)

print("OK: enqueue atomic publish permissions")
