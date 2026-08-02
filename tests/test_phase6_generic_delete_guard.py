import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.image_service import cleanup_image_retention, delete_images, delete_to_target
from services.image_storage_service import ImageStorageService


class Phase6GenericDeleteGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="phase6-generic-delete-"))
        self.images = self.tmp / "images"
        self.images.mkdir()
        self.rel = "2026/08/01/tracked.png"
        target = self.images / self.rel
        target.parent.mkdir(parents=True)
        target.write_bytes(b"tracked-source")
        self.fake_config = SimpleNamespace(
            images_dir=self.images,
            image_thumbnails_dir=self.tmp / "thumbnails",
            receipt_protected_image_paths=lambda: {self.rel},
            get_image_storage_settings=lambda: {"mode": "local"},
            image_retention_days=1,
            base_url="",
        )

    def tearDown(self) -> None:
        for path in sorted(self.tmp.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.tmp.rmdir()

    def test_generic_storage_delete_retains_receipt_tracked_source(self) -> None:
        storage = ImageStorageService(index_file=self.tmp / "index.json")
        with patch("services.config.config", self.fake_config), patch("services.image_storage_service.config", self.fake_config):
            self.assertFalse(storage.delete(self.rel))
        self.assertTrue((self.images / self.rel).exists())

    def test_low_space_cleanup_fails_closed_when_cleanup_state_is_missing(self) -> None:
        with patch("services.image_service.config", self.fake_config), patch(
            "services.image_service.CLEANUP_STATE_FILE", self.tmp / "missing-cleanup.json"
        ), patch("services.config.config", self.fake_config), patch(
            "services.image_storage_service.config", self.fake_config
        ):
            result = delete_to_target(10**9)
        self.assertEqual(result["removed"], 0)
        self.assertTrue((self.images / self.rel).exists())

    def test_retention_cleanup_retains_receipt_tracked_source(self) -> None:
        target = self.images / self.rel
        os.utime(target, (1, 1))
        with patch("services.image_service.config", self.fake_config), patch(
            "services.config.config", self.fake_config
        ), patch("services.image_storage_service.config", self.fake_config):
            result = cleanup_image_retention(1)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(target.exists())

    def test_retention_cleanup_fails_closed_when_cleanup_state_is_missing(self) -> None:
        untracked_rel = "2026/08/01/untracked.png"
        untracked = self.images / untracked_rel
        untracked.write_bytes(b"untracked-source")
        for path in (self.images / self.rel, untracked):
            os.utime(path, (1, 1))
        with patch("services.image_service.config", self.fake_config), patch(
            "services.image_service.CLEANUP_STATE_FILE", self.tmp / "missing-cleanup.json"
        ), patch("services.config.config", self.fake_config), patch(
            "services.image_storage_service.config", self.fake_config
        ):
            result = cleanup_image_retention(1)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(untracked.exists())

    def test_manual_bulk_delete_keeps_thumbnails_for_retained_source(self) -> None:
        thumbnails = self.tmp / "thumbnails"
        candidates = (thumbnails / f"{self.rel}.png", thumbnails / self.rel)
        for thumb in candidates:
            thumb.parent.mkdir(parents=True, exist_ok=True)
            thumb.write_bytes(b"thumb")
        with patch("services.image_service.config", self.fake_config), patch(
            "services.config.config", self.fake_config
        ), patch("services.image_storage_service.config", self.fake_config):
            result = delete_images(paths=[self.rel])
        self.assertEqual(result["removed"], 0)
        self.assertTrue((self.images / self.rel).exists())
        for thumb in candidates:
            self.assertTrue(thumb.exists())

    def test_retention_cleanup_keeps_thumbnails_for_retained_source(self) -> None:
        target = self.images / self.rel
        os.utime(target, (1, 1))
        thumbnails = self.tmp / "thumbnails"
        # Only the canonical ``<rel>.png`` thumbnail survives the orphan
        # sweep that runs after retention cleanup; assert on that one.
        candidates = (thumbnails / f"{self.rel}.png",)
        for thumb in candidates:
            thumb.parent.mkdir(parents=True, exist_ok=True)
            thumb.write_bytes(b"thumb")
        with patch("services.image_service.config", self.fake_config), patch(
            "services.config.config", self.fake_config
        ), patch("services.image_storage_service.config", self.fake_config):
            result = cleanup_image_retention(1)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(target.exists())
        for thumb in candidates:
            self.assertTrue(thumb.exists())

    def test_manual_bulk_delete_retains_receipt_tracked_source(self) -> None:
        with patch("services.image_service.config", self.fake_config), patch(
            "services.config.config", self.fake_config
        ), patch("services.image_storage_service.config", self.fake_config):
            result = delete_images(paths=[self.rel])
        self.assertEqual(result["removed"], 0)
        self.assertTrue((self.images / self.rel).exists())


if __name__ == "__main__":
    unittest.main()
