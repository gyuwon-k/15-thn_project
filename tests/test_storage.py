"""Integration coverage against the bundled private MariaDB runtime."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
import uuid

from backend.storage import Storage, StorageConflict


class StorageIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_root = Path(__file__).resolve().parents[1] / "work" / "storage-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="pilot-v2-storage-", dir=test_root)
        cls.folder = Path(cls.temporary.name)
        resources = Path(os.environ.get("PILOT_RESOURCE_DIR", Path(__file__).resolve().parents[1] / "packaging" / "resources"))
        cls.store = Storage(cls.folder / "data", resources).init()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        assert cls.folder.resolve().is_relative_to(Path(__file__).resolve().parents[1] / "work" / "storage-tests")
        cls.temporary.cleanup()

    def test_01_concurrent_save_rejects_stale_revision(self):
        project = self.store.create_project({"name": "고전압 정션박스", "revision": 8})
        saved = self.store.save_project(dict(project, name="수정"), project["row_version"], {"kind": "renamed"})
        self.assertEqual(saved["revision"], 8)
        self.assertEqual(saved["row_version"], 2)
        with self.assertRaises(StorageConflict):
            self.store.save_project(project, 1)
        self.assertEqual(self.store.get_project(project["id"])["name"], "수정")
        self.assertEqual(len(self.store.events(project["id"])), 1)

    def test_02_backup_restore_preserves_snapshots_and_owned_files(self):
        project = self.store.create_project({"name": "백업 원본"})
        original = self.folder / "모델.step"
        original.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")
        asset = self.store.register_file(original, "whole_step", project["id"])
        original.unlink()
        self.assertTrue(self.store.file_path(asset["id"], True).is_file())
        snapshot = self.store.add_snapshot(project["id"], {"plan": {"operations": ["A", "B"]}, "validation_hash": "test"}, expected_revision=1)
        with self.assertRaises(StorageConflict):
            self.store.add_snapshot(project["id"], {"plan": {}}, expected_revision=0)
        self.store.set_setting("test", {"enabled": True})
        backup = self.store.backup()
        restored_path = self.folder / "restored"
        result = self.store.restore(backup["path"], restored_path)
        self.assertEqual(result["status"], "verified")
        restored = Storage(restored_path, self.store.resource_dir).init()
        try:
            self.assertEqual(restored.get_snapshot(snapshot["id"]), snapshot)
            self.assertEqual(restored.get_setting("test"), {"enabled": True})
            self.assertEqual(restored.file_path(asset["id"], True).read_bytes(), b"ISO-10303-21;\nEND-ISO-10303-21;")
            self.assertNotEqual(restored.status()["binding_id"], self.store.status()["binding_id"])
        finally:
            restored.close()

    def test_03_archive_traversal_rejected(self):
        path = self.folder / "unsafe.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("../outside", "bad")
        with self.assertRaisesRegex(ValueError, "BACKUP_UNSAFE_PATH"):
            self.store.validate_backup(path)

    def test_04_no_silent_fallback(self):
        candidate = Storage(self.folder / "missing-runtime", self.folder / "not-present")
        explicit = os.environ.pop("PILOT_MARIADB_ROOT", None)
        try:
            with self.assertRaisesRegex(RuntimeError, "MARIADB_RUNTIME_MISSING"):
                candidate.init()
            self.assertFalse((candidate.data_dir / "database").exists())
        finally:
            candidate.close()
            if explicit:
                os.environ["PILOT_MARIADB_ROOT"] = explicit

    def test_05_same_storage_cannot_start_twice(self):
        duplicate = Storage(self.store.data_dir, self.store.resource_dir)
        try:
            with self.assertRaisesRegex(RuntimeError, "STORAGE_LOCKED"):
                duplicate.init()
            self.assertEqual(self.store.status()["status"], "connected")
        finally:
            duplicate.close()

    def test_06_verified_restore_selection_is_atomic_and_fails_closed(self):
        from backend.release import selected_data_dir, stage_active_storage
        archive = self.store.backup()
        target = self.folder / "restored" / ("restore-" + uuid.uuid4().hex)
        self.store.restore(archive["path"], target)
        selected = stage_active_storage(target, self.store.data_dir)
        self.assertTrue(selected["restart_required"])
        self.assertEqual(selected_data_dir(self.store.data_dir), target.resolve())
        self.assertEqual(self.store.status()["status"], "connected")
        record_path = target / "restore-result.json"
        record_path.write_text(record_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "ACTIVE_STORAGE_INVALID"):
            selected_data_dir(self.store.data_dir)


if __name__ == "__main__":
    unittest.main()
