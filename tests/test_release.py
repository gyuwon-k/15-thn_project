import json
from pathlib import Path
import tempfile
import unittest

from backend.release import activate_release, active_release, installation_lock, seal_release, verify_release, prepare_browser_runtime


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "work" / "release-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.install = Path(self.temp.name)
        self.template = json.loads((Path(__file__).resolve().parents[1] / "release.json").read_text(encoding="utf-8"))

    def tearDown(self):
        assert self.install.resolve().is_relative_to(Path(__file__).resolve().parents[1] / "work" / "release-tests")
        self.temp.cleanup()

    def release(self, version):
        build = "pilot-" + version + "-test"
        folder = self.install / "releases" / build
        folder.mkdir(parents=True)
        (folder / "payload.bin").write_bytes(version.encode())
        manifest = dict(self.template, app_version=version, build_id=build)
        for component in ("frontend", "backend", "launcher"):
            manifest[component + "_build_id"] = build
        seal_release(folder, manifest)
        return folder, build

    def test_upgrade_activates_actual_new_payload_and_preserves_old(self):
        old, old_build = self.release("2.0.0")
        new, new_build = self.release("2.0.1")
        checks = []
        def healthy(folder, manifest):
            checks.append(manifest["build_id"])
        activate_release(self.install, old_build, health_check=healthy)
        activate_release(self.install, new_build, health_check=healthy)
        active, manifest = active_release(self.install)
        self.assertEqual(active, new)
        self.assertEqual((active / "payload.bin").read_text(), "2.0.1")
        self.assertEqual(checks, [old_build, new_build])
        self.assertTrue(old.is_dir())

    def test_failed_update_preserves_active_pointer(self):
        old, old_build = self.release("2.0.0")
        new, new_build = self.release("2.0.1")
        activate_release(self.install, old_build, health_check=lambda *_: None)
        def fail(*_):
            raise RuntimeError("native dependency missing")
        with self.assertRaisesRegex(RuntimeError, "native dependency missing"):
            activate_release(self.install, new_build, health_check=fail)
        self.assertEqual(active_release(self.install)[0], old)
        journal = json.loads((self.install / "update-journal.json").read_text())
        self.assertEqual(journal["status"], "failed")

    def test_corrupt_release_and_build_mismatch_blocked(self):
        folder, build = self.release("2.0.0")
        (folder / "payload.bin").write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "RELEASE_COMPONENT_CORRUPT"):
            verify_release(folder)
        (folder / "payload.bin").write_bytes(b"2.0.0")
        manifest = json.loads((folder / "release.json").read_text())
        manifest["backend_build_id"] = "old-build"
        (folder / "release.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "RELEASE_BUILD_MISMATCH"):
            verify_release(folder)

    def test_running_app_prevents_update(self):
        folder, build = self.release("2.0.0")
        with installation_lock(self.install):
            with self.assertRaisesRegex(RuntimeError, "APPLICATION_RUNNING"):
                activate_release(self.install, build, health_check=lambda *_: None)

    def test_browser_copy_preserves_source_and_rechecks_every_executable(self):
        source = self.install / "source" / "browsers"
        browser = source / "chromium-1"
        browser.mkdir(parents=True)
        (browser / "chrome.exe").write_bytes(b"runtime-binary")
        (browser / "support.dll").write_bytes(b"native-library")
        (browser / "debug.log").write_bytes(b"old runtime log")
        data = self.install / "data"
        copy = prepare_browser_runtime(source, data, "test-build-1")
        self.assertFalse((copy / "chromium-1" / "debug.log").exists())
        (copy / "chromium-1" / "debug.log").write_bytes(b"GPU runtime log")
        self.assertEqual(prepare_browser_runtime(source, data, "test-build-1"), copy)
        self.assertEqual((browser / "debug.log").read_bytes(), b"old runtime log")
        (copy / "chromium-1" / "support.dll").write_bytes(b"tampered-library")
        with self.assertRaisesRegex(RuntimeError, "BROWSER_RUNTIME_CHANGED"):
            prepare_browser_runtime(source, data, "test-build-1")

    def test_browser_copy_publication_is_atomic_under_concurrent_preparation(self):
        from concurrent.futures import ThreadPoolExecutor
        source = self.install / "source"
        browser = source / "chromium-1"
        browser.mkdir(parents=True)
        (browser / "chrome.exe").write_bytes(b"runtime-binary")
        with ThreadPoolExecutor(max_workers=3) as pool:
            copies = list(pool.map(lambda _: prepare_browser_runtime(source, self.install / "data", "test-build-2"), range(3)))
        self.assertEqual(len(set(copies)), 1)
        self.assertTrue((copies[0] / "runtime-ready.json").is_file())
        (copies[0] / "injected.dll").write_bytes(b"unregistered module")
        with self.assertRaisesRegex(RuntimeError, "UNREGISTERED_EXECUTABLE"):
            prepare_browser_runtime(source, self.install / "data", "test-build-2")


if __name__ == "__main__":
    unittest.main()
