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
        self.config_patch = None
        self.claim_path_patch = None
        self.staging_env_patch = None
        self.tmp = Path(tempfile.mkdtemp(prefix="image-storage-cleanup-"))
        self.images = self.tmp / "images"
        self.protected_staging = self.tmp / "protected-staging"
        self.protected_staging.mkdir()
        if os.name != "nt":
            try:
                os.chown(self.protected_staging, 65534, 65534)
            except (AttributeError, PermissionError, OSError):
                self.skipTest("protected POSIX staging requires a provisioned ownership boundary")
            self.staging_env_patch = patch.dict(
                os.environ,
                {"GENBOX_CLEANUP_PROTECTED_STAGING_ROOT": str(self.protected_staging)},
            )
            self.staging_env_patch.start()
        else:
            self.staging_env_patch = None
        self.storage = ImageStorageService(index_file=self.tmp / "index.json")
        self.config_patch = patch("services.image_storage_service.config", _ImageConfig(self.images))
        self.config_patch.start()
        # Keep the process-shared source claim inside this test's temporary
        # root so a live development server cannot make the fixture appear
        # busy or mask the storage identity result.
        self.claim_path_patch = patch("services.source_claim.DATA_DIR", self.tmp / "claims")
        self.claim_path_patch.start()

    def tearDown(self) -> None:
        if self.staging_env_patch is not None:
            self.staging_env_patch.stop()
        if self.config_patch is not None:
            self.config_patch.stop()
        if self.claim_path_patch is not None:
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

    def _identity(self, rel: str, digest: str) -> dict[str, int]:
        identity = self.storage.verify_local_identity(rel, digest)
        self.assertTrue(identity.get("ok"), identity)
        return dict(identity["source_identity"])

    def test_replacement_after_verification_is_retained(self) -> None:
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
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
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

        if replacement_blocked:
            self.assertEqual(result.status, "deleted")
            self.assertFalse(target.exists())
        else:
            self.assertEqual(result.status, "retained")
            self.assertEqual(result.reason, "source-changed")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"replacement-bytes")

    def test_same_content_different_identity_is_retained(self) -> None:
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        replacement = target.with_name("same-content-replacement.png")
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)

        result = self.storage.delete_verified_local(
            rel,
            digest,
            expected_identity=source_identity,
        )

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "source-changed")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"source-bytes")

    def test_replacement_after_final_identity_check_is_retained(self) -> None:
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        replacement = target.with_name("replacement-after-final-check.png")
        replacement_blocked = []

        def replace_after_final_check(_opened: object) -> None:
            replacement.write_bytes(b"replacement-after-final-check")
            try:
                os.replace(replacement, target)
            except OSError as exc:
                replacement_blocked.append(exc)

        with patch.object(self.storage, "_after_final_identity_check", side_effect=replace_after_final_check):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)
        if replacement_blocked:
            self.assertEqual(result.status, "deleted")
            self.assertFalse(target.exists())
        else:
            self.assertEqual(result.status, "retained")
            self.assertIn(result.reason, {"source-changed", "path-alias", "atomic-delete-failed"})
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"replacement-after-final-check")

    def test_parent_directory_move_after_final_identity_check_is_retained(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX parent-directory anchoring requires Linux")
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        original_parent = target.parent
        moved_parent = original_parent.with_name(original_parent.name + "-moved")

        def move_parent(_opened: object) -> None:
            original_parent.rename(moved_parent)
            original_parent.mkdir()
            (original_parent / target.name).write_bytes(b"replacement-bytes")

        with patch.object(self.storage, "_after_final_identity_check", side_effect=move_parent):
            result = self.storage.delete_verified_local(
                rel,
                digest,
                expected_identity=source_identity,
            )

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "source-changed")
        self.assertEqual(result.detail, "restored-original-entry")
        self.assertTrue((moved_parent / target.name).exists())
        self.assertEqual((moved_parent / target.name).read_bytes(), b"source-bytes")
        self.assertEqual(target.read_bytes(), b"replacement-bytes")

    def test_parent_directory_aba_before_exchange_is_retained(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX parent-directory ABA detection requires Linux")
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        moved_parent = target.parent.with_name(target.parent.name + "-moved")
        original_anchor_check = self.storage._cleanup_parent_is_anchored

        def move_away_and_back(opened: object) -> bool:
            anchored = original_anchor_check(opened)
            self.assertTrue(anchored)
            target.parent.rename(moved_parent)
            moved_parent.rename(target.parent)
            return anchored

        with patch.object(self.storage, "_cleanup_parent_is_anchored", side_effect=move_away_and_back):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "source-parent-changed")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"source-bytes")

    def test_unknown_posix_mount_identity_is_not_anchored(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mount identity requires Linux")
        rel, _target, digest = self._source()
        source_identity = self._identity(rel, digest)
        opened = self.storage._open_verified_cleanup_target(
            rel,
            digest,
            expected_identity=source_identity,
            write_guard=False,
        )
        self.assertFalse(isinstance(opened, dict), opened)
        try:
            with patch.object(self.storage, "_descriptor_mount_id", return_value=None):
                self.assertFalse(self.storage._cleanup_parent_is_anchored(opened))
        finally:
            self.storage._close_cleanup_target(opened)

    def test_staging_replacement_is_not_unlinked(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX staging race requires Linux")
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        victim = self.protected_staging / "unrelated-victim"
        victim.write_bytes(b"unrelated-victim")
        staging_descriptors: list[int] = []
        real_open_staging = ImageStorageService._open_protected_cleanup_staging
        real_unlink = os.unlink

        def capture_staging(parent_fd: int) -> int | None:
            descriptor = real_open_staging(parent_fd)
            if descriptor is not None:
                staging_descriptors.append(descriptor)
            return descriptor

        def replace_before_legacy_unlink(name: str, *, dir_fd: int | None = None) -> None:
            if (
                staging_descriptors
                and dir_fd == staging_descriptors[0]
                and name.endswith(".observed")
            ):
                observed = Path(f"/proc/self/fd/{dir_fd}").resolve() / name
                os.replace(victim, observed)
            real_unlink(name, dir_fd=dir_fd)

        with patch.object(self.storage, "_open_protected_cleanup_staging", side_effect=capture_staging), patch(
            "services.image_storage_service.os.unlink", side_effect=replace_before_legacy_unlink,
        ):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

        self.assertEqual(result.status, "deleted")
        self.assertFalse(target.exists())
        self.assertTrue(victim.exists())
        self.assertEqual(victim.read_bytes(), b"unrelated-victim")

    def test_hard_link_added_after_final_identity_check_is_retained(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX hard-link race requires a Linux filesystem")
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        alias = target.with_name("alias-after-final-check.png")

        def add_alias(_opened: object) -> None:
            os.link(target, alias)

        with patch.object(self.storage, "_after_final_identity_check", side_effect=add_alias):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "path-alias")
        self.assertTrue(target.exists())
        self.assertTrue(alias.exists())

    def test_same_inode_same_size_rewrite_after_final_identity_check_is_retained(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX in-place rewrite race requires a Linux filesystem")
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        replacement = b"mutated-data"
        self.assertEqual(len(replacement), target.stat().st_size)

        def rewrite_after_final_check(_opened: object) -> None:
            target.write_bytes(replacement)

        with patch.object(self.storage, "_after_final_identity_check", side_effect=rewrite_after_final_check):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "source-changed")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), replacement)

    def test_same_inode_rewrite_after_posix_tombstone_is_restored(self) -> None:
        if os.name == "nt" or not hasattr(os, "pwrite"):
            self.skipTest("POSIX descriptor rewrite race requires Linux pwrite")
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        replacement = b"mutated-data"
        self.assertEqual(len(replacement), target.stat().st_size)

        def rewrite_after_tombstone(opened: object) -> None:
            os.pwrite(opened.descriptor, replacement, 0)
            os.fsync(opened.descriptor)

        with patch.object(self.storage, "_after_posix_tombstone", side_effect=rewrite_after_tombstone):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

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
        source_identity = self._identity(rel, digest)
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
                result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)
            self.assertIsNotNone(writer)
            writer.join(timeout=15)
            if writer.is_alive():
                writer.terminate()
                writer.join(timeout=5)
            writer_result = result_queue.get(timeout=5)
            if result.status == "deleted":
                self.assertFalse(target.exists())
                # The racer's file must never be the object that was deleted:
                # when the path write landed after the verified inode moved
                # into staging, the replacement stays behind at the path.
                if writer_result == "wrote":
                    self.assertTrue(target.exists())
                    self.assertEqual(target.read_bytes(), b"mutated-data")
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
        source_identity = self._identity(rel, digest)
        alias = target.with_name("alias-during-exchange.png")

        def add_alias(_opened: object) -> None:
            os.link(target, alias)

        with patch.object(self.storage, "_before_posix_exchange", side_effect=add_alias):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

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
        source_identity = self._identity(rel, digest)
        replacement = target.with_name("replacement-during-exchange.png")

        def replace_entry(_opened: object) -> None:
            replacement.write_bytes(b"replacement-during-exchange")
            os.replace(replacement, target)

        with patch.object(self.storage, "_before_posix_exchange", side_effect=replace_entry):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)
        # The racer's file is restored at the recorded name and the original
        # inode is retained under the protected staging boundary.
        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "source-changed")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"replacement-during-exchange")
        staged = [
            path
            for path in self.protected_staging.iterdir()
            if path.name.endswith(".staged")
        ]
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].read_bytes(), b"source-bytes")
        self.assertIn(staged[0].name, result.detail)

    def test_parent_move_during_posix_exchange_drops_owned_temp_link(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX exchange race requires a Linux filesystem")
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        moved_parent = target.parent.with_name(target.parent.name + "-moved")

        def move_parent(_opened: object) -> None:
            target.parent.rename(moved_parent)

        with patch.object(self.storage, "_before_posix_exchange", side_effect=move_parent):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

        self.assertEqual(result.status, "retained")
        self.assertEqual(result.reason, "source-parent-changed")
        moved_source = moved_parent / target.name
        self.assertTrue(moved_source.exists())
        self.assertEqual(moved_source.read_bytes(), b"source-bytes")
        leftovers = [
            path.name
            for path in moved_parent.iterdir()
            if path.name.startswith((".genbox-cleanup-", ".genbox-retained-"))
        ]
        self.assertEqual(leftovers, [])

    def test_hard_link_alias_is_retained(self) -> None:
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        alias = target.with_name("alias.png")
        try:
            os.link(target, alias)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hard links unavailable: {exc}")

        result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

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

        outside_stat = (outside / "source.png").stat()
        source_identity = {
            "device": int(outside_stat.st_dev), "inode": int(outside_stat.st_ino),
            "size_bytes": int(outside_stat.st_size),
        }
        result = self.storage.delete_verified_local(
            rel,
            hashlib.sha256(b"outside-bytes").hexdigest(),
            expected_identity=source_identity,
        )

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
            outside_stat = (outside / "source.png").stat()
            source_identity = {
                "device": int(outside_stat.st_dev), "inode": int(outside_stat.st_ino),
                "size_bytes": int(outside_stat.st_size),
            }
            result = self.storage.delete_verified_local(
                rel,
                hashlib.sha256(b"outside-bytes").hexdigest(),
                expected_identity=source_identity,
            )
            self.assertEqual(result.status, "retained")
            self.assertEqual(result.reason, "path-alias")
            self.assertTrue((outside / "source.png").exists())
        finally:
            os.rmdir(alias_dir)

    def test_index_write_failure_is_delete_unknown(self) -> None:
        rel, target, digest = self._source()
        source_identity = self._identity(rel, digest)
        self.storage._save_index({rel: {"rel": rel, "local": True, "webdav": False}})
        with patch.object(self.storage, "_save_index", side_effect=OSError("index unavailable")):
            result = self.storage.delete_verified_local(rel, digest, expected_identity=source_identity)

        self.assertEqual(result.status, "delete_unknown")
        self.assertEqual(result.reason, "index-write-failed")
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
