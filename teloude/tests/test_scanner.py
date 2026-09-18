# teloude/tests/test_scanner.py
"""Scanner + duplicate detection tests (read-only behavior, determinism)."""
import os

import pytest

from teloude.core.scanner import (
    ScanError,
    compute_fingerprint,
    hash_scanned,
    scan_directory,
    sha256_of,
)
from teloude.core.duplicates import (
    DuplicateAction,
    DuplicateDecision,
    DuplicateMatch,
    DuplicateResolver,
    find_duplicates,
)
from teloude.core.scanner import ScannedFile


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_bytes(b"hello world")
    (tmp_path / "docs" / "b.bin").write_bytes(bytes(range(256)) * 40)
    (tmp_path / "pics" / "sub").mkdir(parents=True)
    (tmp_path / "pics" / "sub" / "c.jpg").write_bytes(b"\xff\xd8" + b"0" * 100)
    (tmp_path / "ünïcode name.txt").write_bytes(b"unicode ok")
    return tmp_path


class TestScanner:
    def test_scan_lists_all_files_sorted(self, tree):
        found = scan_directory(tree)
        assert [s.relative for s in found] == [
            "docs/a.txt",
            "docs/b.bin",
            "pics/sub/c.jpg",
            "ünïcode name.txt",
        ]

    def test_metadata_and_fingerprint(self, tree):
        found = {s.relative: s for s in scan_directory(tree)}
        a = found["docs/a.txt"]
        assert a.size == 11 and a.name == "a.txt"
        assert a.fingerprint == compute_fingerprint(a.size, a.mtime_ns)
        assert a.sha256 is None  # hashes are on demand only

    def test_hashing_is_streaming_and_stable(self, tree):
        target = tree / "docs" / "b.bin"
        h1 = sha256_of(target, chunk_size=64)
        h2 = sha256_of(target)
        assert h1 == h2 and len(h1) == 64
        scanned = scan_directory(tree)[0]
        assert hash_scanned(scanned).sha256 is not None

    def test_missing_root_raises(self, tmp_path):
        with pytest.raises(ScanError):
            scan_directory(tmp_path / "nope")

    def test_file_as_root_raises(self, tree):
        with pytest.raises(ScanError):
            scan_directory(tree / "docs" / "a.txt")

    def test_source_files_never_modified(self, tree):
        target = tree / "docs" / "a.txt"
        before = (target.stat().st_mtime_ns, target.read_bytes())
        scan_directory(tree)
        sha256_of(target)
        assert (target.stat().st_mtime_ns, target.read_bytes()) == before

    def test_inaccessible_dir_skipped_with_callback(self, tree):
        errors = []
        blocked = tree / "docs"
        os.chmod(blocked, 0)
        try:
            try:
                os.scandir(blocked).__enter__()
            except OSError:
                pass  # permissions enforced -> proceed with the test
            else:
                pytest.skip("platform ignores directory permissions (elevated user?)")
            found = scan_directory(tree, on_error=lambda p, e: errors.append((p, e)))
        finally:
            os.chmod(blocked, 0o755)
        assert errors, "inaccessible directory must be reported"
        assert all("docs" not in s.relative for s in found)

    def test_symlink_not_followed(self, tree, tmp_path):
        link = tree / "loop"
        try:
            link.symlink_to(tree, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
        found = scan_directory(tree)
        assert all(not s.relative.startswith("loop") for s in found)


class TestDuplicates:
    def _scanned(self, name="a.txt", sha="abc"):
        return ScannedFile(
            path=None, relative=name, name=name, size=1, mtime_ns=1,
            fingerprint="1:1", sha256=sha,
        )

    def test_no_sha_no_match(self):
        item = self._scanned()
        item.sha256 = None
        assert find_duplicates([item], lambda s: [{"storage": "S", "relative_path": "x"}]) == []

    def test_match_reported_once(self):
        matches = find_duplicates(
            [self._scanned()], lambda s: [{"storage": "Photos", "relative_path": "a.txt"}]
        )
        assert len(matches) == 1
        assert matches[0].existing_storage == "Photos"

    def test_skip_all_policy(self):
        resolver = DuplicateResolver(policy=DuplicateResolver.SKIP_ALL)
        match = DuplicateMatch(self._scanned(), "S", "a.txt")
        assert resolver.decide(match, 1, 1) is DuplicateAction.SKIP

    def test_upload_all_policy(self):
        resolver = DuplicateResolver(policy=DuplicateResolver.UPLOAD_ALL)
        match = DuplicateMatch(self._scanned(), "S", "a.txt")
        assert resolver.decide(match, 1, 1) is DuplicateAction.UPLOAD_AGAIN

    def test_ask_with_apply_to_all_remembered(self):
        calls = []

        def ask(match, i, n):
            calls.append((i, n))
            return DuplicateDecision(DuplicateAction.SKIP, apply_to_all=True)

        resolver = DuplicateResolver(ask_callback=ask)
        match = DuplicateMatch(self._scanned(), "S", "a.txt")
        assert resolver.decide(match, 1, 3) is DuplicateAction.SKIP
        assert resolver.decide(match, 2, 3) is DuplicateAction.SKIP
        assert len(calls) == 1  # second answer reused, no second prompt

    def test_cancel_not_remembered(self):
        answers = [
            DuplicateDecision(DuplicateAction.CANCEL, apply_to_all=True),
            DuplicateDecision(DuplicateAction.SKIP),
        ]
        resolver = DuplicateResolver(ask_callback=lambda m, i, n: answers.pop(0))
        match = DuplicateMatch(self._scanned(), "S", "a.txt")
        assert resolver.decide(match, 1, 2) is DuplicateAction.CANCEL
        assert resolver.decide(match, 2, 2) is DuplicateAction.SKIP

    def test_ask_requires_callback(self):
        with pytest.raises(ValueError):
            DuplicateResolver(policy=DuplicateResolver.ASK)
