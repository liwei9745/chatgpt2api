from __future__ import annotations

import hashlib
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.image_storage_service import ImageStorageService


def _posix_write_attempt_worker(path: str, started_queue, result_queue) -> None:
    started_queue.put("started")
    try:
        Path(path).write_bytes(b"mutated-data")
    except OSError as exc:
        result_queue.put(f"failed:{type(exc).__name__}")
    else:
        result_queue.put("wrote")


class _ImageConfig:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def images_dir(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def get_image_storage_settings(self) -> dict[str, object]:
        return {"mode": "local"}

    @property
    def base_url(self) -> str:
        return ""


class ImageStorageCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="image-storage-cleanup-"))
        self.images = self.tmp / "images"
        self.storage = ImageStorageService(index_file=self.tmp / "index.json")
        self.config_patch = patch("services.image_storage_service.config", _ImageConfig(self.images))
        self.config_patch.start()
        # Keep the process-shared source claim inside this test's temporary
        # root so a live development server cannot make the fixture appear
        # busy or mask the storage identity result.
        self.claim_path_patch = patch("services.source_claim.DATA_DIR", self.tmp / "claims")
        self.claim_path_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.claim_path_patch.stop()
        for path in sorted(self.tmp.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.tmp.rmdir()

    def _source(self, payload: bytes = b"source-bytes") -> tuple[str, Path, str]:
        rel = "2026/08/01/source.png"
        target = self.images / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return rel, target, hashlib.sha256(payload).hexdigest()

    def test_replacement_after_verification_is_retained(self) -> None:
        rel, target, digest = self._source()
        replacement = target.with_name("replacement.png")
        replacement_blocked = []

        def replace_after_hash(_opened: object) -> None:
            replacement.write_bytes(b"replacement-bytes")
            try:
                os.replace(replacement, target)
            except OSError as exc:
                # Windows filesystems may reject a rename even with delete
                # sharing. The exact-handle delete fence intentionally blocks
                # the replacement, which is itself the expected safe result.
                replacement_blocked.append(exc)

        with patch.object(self.storage, "_before_verified_unlink", side_effect=replace_after_hash):
            result = self.storage.delete_verified_local(rel, digest)

        if replacement_blocked:
            self.assertEqual(result.status, "deleted")
            self.assertFalse(target.exists())
        else:
            self.assertEqual(result.status, "retained")
            self.assertEqual(result.reason, "source-changed")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"replacement-bytes")

    def test_replacement_after_final_identity_check_is_retained(self) -> None:
        rel, target, digest = self._source()
        replacement = target.with_name("replacement-after-final-check.png")
        replacement_blocked = []

        def replace_after_final_check(_opened: object) -> None:
            replacement.write_bytes(b"replacement-after-final-check")
            try:
                os.replace(replacement, target)
            except OSError as exc:
                replacement_blocked.append(exc)

        with patch.object(self.storage, "_after_final_identity_check", side_effect=replace_after_final_check):
            result = self.storage.delete_verified_local(rel, digest)

        if replacement_blocked:
            self.assertEqual(result.status, "deleted")
            self.assertFalse(target.exists())
        else:
            self.assertEqual(result.status, "retained")
            self.assertIn(result.reason, {"source-changed", "path-alias", "atomic-delete-failed"})
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"replacement-after-final-check")

    def test_hard_link_added_after_final_identity_check_is_retained(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX hard-link race requires a Linux filesystem")
        rel, target, digest = self._source()
        alias = target.with_name("alias-after-final-check.png")

        def add_alias(_opened: object) -> None:
            os.link(target, alias)

        with patch.object(self.storage, "_after_final_identity_check", side_effect=add_alias):
            result = self.storage.delete_verified_local(rel, digest)

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "path-alias")
        self.assertTrue(target.exists())
        self.assertTrue(alias.exists())

    def test_same_inode_same_size_rewrite_after_final_identity_check_is_retained(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX in-place rewrite race requires a Linux filesystem")
        rel, target, digest = self._source()
        replacement = b"mutated-data"
        self.assertEqual(len(replacement), target.stat().st_size)

        def rewrite_after_final_check(_opened: object) -> None:
            target.write_bytes(replacement)

        with patch.object(self.storage, "_after_final_identity_check", side_effect=rewrite_after_final_check):
            result = self.storage.delete_verified_local(rel, digest)

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "source-changed")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), replacement)

    def test_same_inode_rewrite_after_posix_tombstone_is_restored(self) -> None:
        if os.name == "nt" or not hasattr(os, "pwrite"):
            self.skipTest("POSIX descriptor rewrite race requires Linux pwrite")
        rel, target, digest = self._source()
        replacement = b"mutated-data"
        self.assertEqual(len(replacement), target.stat().st_size)

        def rewrite_after_tombstone(opened: object) -> None:
            os.pwrite(opened.descriptor, replacement, 0)
            os.fsync(opened.descriptor)

        with patch.object(self.storage, "_after_posix_tombstone", side_effect=rewrite_after_tombstone):
            result = self.storage.delete_verified_local(rel, digest)

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "source-changed")
        self.assertEqual(result.detail, "restored-original-entry")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), replacement)
        leftovers = [
            path.name
            for path in target.parent.iterdir()
            if path.name.startswith((".genbox-cleanup-", ".genbox-retained-"))
        ]
        self.assertEqual(leftovers, [])

    def test_cross_process_writer_cannot_turn_delete_into_changed_source(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX write-lease race requires Linux")
        rel, target, digest = self._source()
        context = multiprocessing.get_context("spawn")
        started_queue = context.Queue()
        result_queue = context.Queue()
        writer = None

        def start_writer(_opened: object) -> None:
            nonlocal writer
            writer = context.Process(
                target=_posix_write_attempt_worker,
                args=(str(target), started_queue, result_queue),
            )
            writer.start()
            self.assertEqual(started_queue.get(timeout=5), "started")

        try:
            with patch.object(self.storage, "_before_posix_exchange", side_effect=start_writer):
                result = self.storage.delete_verified_local(rel, digest)
            self.assertIsNotNone(writer)
            writer.join(timeout=15)
            if writer.is_alive():
                writer.terminate()
                writer.join(timeout=5)
            writer_result = result_queue.get(timeout=5)
            if result.status == "deleted":
                self.assertNotEqual(writer_result, "wrote")
                self.assertFalse(target.exists())
            else:
                self.assertEqual(result.status, "retained")
                self.assertTrue(target.exists())
                self.assertEqual(target.read_bytes(), b"mutated-data")
        finally:
            if writer is not None and writer.is_alive():
                writer.terminate()
                writer.join(timeout=5)

    def test_hard_link_added_during_posix_exchange_restores_original_entry(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX exchange race requires a Linux filesystem")
        rel, target, digest = self._source()
        alias = target.with_name("alias-during-exchange.png")

        def add_alias(_opened: object) -> None:
            os.link(target, alias)

        with patch.object(self.storage, "_before_posix_exchange", side_effect=add_alias):
            result = self.storage.delete_verified_local(rel, digest)

        # The ambiguous source is retained exactly where it was: the original
        # directory entry is restored and only the service's own links are
        # dropped. No temporary or quarantine name may be left behind.
        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "path-alias")
        self.assertEqual(result.detail, "restored-original-entry")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"source-bytes")
        self.assertTrue(alias.exists())
        leftovers = [
            path.name
            for path in target.parent.iterdir()
            if path.name.startswith((".genbox-cleanup-", ".genbox-retained-"))
        ]
        self.assertEqual(leftovers, [])

    def test_replacement_during_posix_exchange_restores_replacement_and_quarantines_source(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX exchange race requires a Linux filesystem")
        rel, target, digest = self._source()
        replacement = target.with_name("replacement-during-exchange.png")

        def replace_entry(_opened: object) -> None:
            replacement.write_bytes(b"replacement-during-exchange")
            os.replace(replacement, target)

        with patch.object(self.storage, "_before_posix_exchange", side_effect=replace_entry):
            result = self.storage.delete_verified_local(rel, digest)

        # The racer's file is restored at the recorded name and the original
        # inode is retained under one opaque quarantine name, which the
        # result detail must disclose for the cleanup audit trail.
        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "source-changed")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"replacement-during-exchange")
        quarantined = [
            path
            for path in target.parent.iterdir()
            if path.name.startswith(".genbox-retained-")
        ]
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b"source-bytes")
        self.assertIn("quarantined:", result.detail)
        self.assertIn(quarantined[0].name, result.detail)
        leftovers = [
            path.name
            for path in target.parent.iterdir()
            if path.name.startswith(".genbox-cleanup-")
        ]
        self.assertEqual(leftovers, [])

    def test_hard_link_alias_is_retained(self) -> None:
        rel, target, digest = self._source()
        alias = target.with_name("alias.png")
        try:
            os.link(target, alias)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hard links unavailable: {exc}")

        result = self.storage.delete_verified_local(rel, digest)

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "path-alias")
        self.assertTrue(target.exists())
        self.assertTrue(alias.exists())

    def test_directory_symlink_alias_is_retained(self) -> None:
        rel = "2026/08/01/source.png"
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "source.png").write_bytes(b"outside-bytes")
        alias_dir = self.images / "2026/08/01"
        alias_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            alias_dir.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        result = self.storage.delete_verified_local(rel, hashlib.sha256(b"outside-bytes").hexdigest())

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "path-alias")
        self.assertTrue((outside / "source.png").exists())

    def test_directory_junction_alias_is_retained(self) -> None:
        rel = "2026/08/01/source.png"
        outside = self.tmp / "junction-outside"
        outside.mkdir()
        (outside / "source.png").write_bytes(b"outside-bytes")
        alias_dir = self.images / "2026/08/01"
        alias_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(alias_dir), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            self.skipTest(f"junction tool unavailable: {exc}")
        if created.returncode != 0 or not alias_dir.exists():
            self.skipTest(f"junction unavailable: {created.stderr.strip() or created.stdout.strip()}")
        try:
            result = self.storage.delete_verified_local(
                rel,
                hashlib.sha256(b"outside-bytes").hexdigest(),
            )
            self.assertEqual(result.status, "retained")
            self.assertEqual(result.reason, "path-alias")
            self.assertTrue((outside / "source.png").exists())
        finally:
            os.rmdir(alias_dir)

    def test_index_write_failure_is_delete_unknown(self) -> None:
        rel, target, digest = self._source()
        self.storage._save_index({rel: {"rel": rel, "local": True, "webdav": False}})
        with patch.object(self.storage, "_save_index", side_effect=OSError("index unavailable")):
            result = self.storage.delete_verified_local(rel, digest)

        self.assertEqual(result.status, "delete_unknown")
        self.assertEqual(result.reason, "index-write-failed")
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
