#!/usr/bin/env python3
import importlib.util
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "libexec" / "artifacts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("kiln_artifacts", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load artifacts module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifacts = load_module()


def test_collects_globs_and_preserves_layout():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        dest = root / "dest"
        (workspace / "dist" / "nested").mkdir(parents=True)
        (workspace / "dist" / "app.js").write_text("app", encoding="utf-8")
        (workspace / "dist" / "nested" / "chunk.js").write_text("chunk", encoding="utf-8")
        (workspace / "coverage").mkdir()
        (workspace / "coverage" / "index.html").write_text("coverage", encoding="utf-8")

        collected = artifacts.collect_artifacts(
            workspace,
            ["dist/**", "coverage/index.html"],
            dest,
        )

        assert collected == ["coverage/index.html", "dist/app.js", "dist/nested/chunk.js"]
        assert (dest / "dist" / "app.js").read_text() == "app"
        assert (dest / "dist" / "nested" / "chunk.js").read_text() == "chunk"
        assert (dest / "coverage" / "index.html").read_text() == "coverage"


def test_each_declared_pattern_must_match_a_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        try:
            artifacts.collect_artifacts(workspace, ["missing/*.zip"], root / "dest")
        except artifacts.ArtifactError as exc:
            assert "matched no files" in str(exc)
        else:
            raise AssertionError("expected missing artifact pattern to fail")


def test_rejects_symlinks_even_when_target_is_inside_workspace():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        (workspace / "dist").mkdir(parents=True)
        (workspace / "real.bin").write_text("ok", encoding="utf-8")
        os.symlink("../real.bin", workspace / "dist" / "link.bin")
        try:
            artifacts.collect_artifacts(workspace, ["dist/*"], root / "dest")
        except artifacts.ArtifactError as exc:
            assert "symlink" in str(exc)
        else:
            raise AssertionError("expected symlink artifact to fail")


def test_resolve_input_roots_keeps_producers_separate():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        for producer in ("linux", "windows"):
            path = build / "artifacts" / producer
            path.mkdir(parents=True)
            (path / f"{producer}.txt").write_text(producer, encoding="utf-8")

        roots = artifacts.resolve_input_roots(build, ["linux", "windows"])
        assert roots == {
            "linux": build / "artifacts" / "linux",
            "windows": build / "artifacts" / "windows",
        }


def test_missing_input_artifacts_fail_clearly():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        try:
            artifacts.resolve_input_roots(build, ["linux"])
        except artifacts.ArtifactError as exc:
            assert "linux" in str(exc)
        else:
            raise AssertionError("expected missing input artifacts to fail")


def main():
    tests = [
        test_collects_globs_and_preserves_layout,
        test_each_declared_pattern_must_match_a_file,
        test_rejects_symlinks_even_when_target_is_inside_workspace,
        test_resolve_input_roots_keeps_producers_separate,
        test_missing_input_artifacts_fail_clearly,
    ]
    for test in tests:
        test()
        print(f"OK artifacts: {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
