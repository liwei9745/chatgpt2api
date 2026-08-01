from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.image_storage_service import ImageStorageService


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

        with patch.object(self.storage, "_before_final_unlink", side_effect=replace_after_final_check):
            result = self.storage.delete_verified_local(rel, digest)

        if replacement_blocked:
            self.assertEqual(result.status, "deleted")
            self.assertFalse(target.exists())
        else:
            self.assertEqual(result.status, "retained")
            self.assertEqual(result.reason, "source-changed")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"replacement-after-final-check")

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
