import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from moment_retrieval.preview_cache import PreviewCache


class PreviewCacheTest(unittest.TestCase):
    def test_cache_key_detects_same_size_same_mtime_content_change(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "source.mp4"
            cache = PreviewCache(root / "previews")
            fixed_stamp = 1_700_000_000_000_000_000
            source.write_bytes(b"A" * (64 * 1024 + 17))
            os.utime(source, ns=(fixed_stamp, fixed_stamp))
            before = cache.cache_path(
                "preview", "synthetic", str(source), [(1.0, 2.0)]
            )

            source.write_bytes(b"B" * (64 * 1024 + 17))
            os.utime(source, ns=(fixed_stamp, fixed_stamp))
            after = cache.cache_path(
                "preview", "synthetic", str(source), [(1.0, 2.0)]
            )

            self.assertNotEqual(before, after)

    def test_same_key_render_is_serialized_and_published_once(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            cache = PreviewCache(Path(temporary_dir) / "previews")
            output = cache.directory / "preview_same.mp4"
            start = threading.Barrier(3)
            render_count = 0
            count_lock = threading.Lock()

            def renderer(temporary):
                nonlocal render_count
                with count_lock:
                    render_count += 1
                time.sleep(0.1)
                temporary.write_bytes(b"complete")

            def create():
                start.wait(timeout=2)
                return cache.create(output, renderer)

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(create) for _ in range(2)]
                start.wait(timeout=2)
                results = [future.result(timeout=3) for future in futures]

            self.assertEqual(results, [str(output), str(output)])
            self.assertEqual(render_count, 1)
            self.assertEqual(output.read_bytes(), b"complete")

    def test_different_lock_stripes_render_in_parallel(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            cache = PreviewCache(Path(temporary_dir) / "previews")
            first = cache.directory / "preview_parallel_a.mp4"
            second = None
            for index in range(1000):
                candidate = cache.directory / f"preview_parallel_{index}.mp4"
                if cache.lock_for(candidate) is not cache.lock_for(first):
                    second = candidate
                    break
            self.assertIsNotNone(second)
            render_barrier = threading.Barrier(2)

            def create(output):
                def renderer(temporary):
                    render_barrier.wait(timeout=2)
                    temporary.write_bytes(output.name.encode("ascii"))

                return cache.create(output, renderer)

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(create, first), pool.submit(create, second)]
                results = [future.result(timeout=3) for future in futures]

            self.assertEqual(results, [str(first), str(second)])
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_failed_render_removes_temp_without_publishing_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            cache = PreviewCache(Path(temporary_dir) / "previews")
            output = cache.directory / "preview_atomic.mp4"

            def fail_after_partial_write(temporary):
                temporary.write_bytes(b"partial")
                raise RuntimeError("synthetic renderer failure")

            with self.assertRaisesRegex(RuntimeError, "synthetic renderer failure"):
                cache.create(output, fail_after_partial_write)

            self.assertFalse(output.exists())
            self.assertEqual(list(cache.directory.glob("*.tmp.mp4")), [])
            self.assertEqual(cache.protected_outputs, set())

    def test_cache_hit_touches_output_without_rendering(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            cache = PreviewCache(Path(temporary_dir) / "previews")
            cache.directory.mkdir()
            output = cache.directory / "preview_hit.mp4"
            output.write_bytes(b"cached")
            os.utime(output, (100, 100))
            before = output.stat().st_mtime_ns

            def must_not_render(_temporary):
                self.fail("cache hit invoked renderer")

            self.assertEqual(cache.create(output, must_not_render), str(output))
            self.assertGreater(output.stat().st_mtime_ns, before)

    def test_protected_output_survives_lru_prune(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            cache = PreviewCache(
                Path(temporary_dir), max_files=1, max_bytes=100
            )
            protected = cache.directory / "preview_protected.mp4"
            removable = cache.directory / "preview_removable.mp4"
            protected.write_bytes(b"old")
            removable.write_bytes(b"new")
            os.utime(protected, (100, 100))
            os.utime(removable, (200, 200))
            cache.protected_outputs.add(protected)

            cache.prune()

            self.assertTrue(protected.exists())
            self.assertFalse(removable.exists())

    def test_startup_cleanup_removes_only_stale_temp_files(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            cache = PreviewCache(Path(temporary_dir), temp_max_age_sec=60)
            stale = cache.directory / "preview_old.token.tmp.mp4"
            recent = cache.directory / "preview_recent.token.tmp.mp4"
            ordinary = cache.directory / "unmanaged.mp4"
            for path in (stale, recent, ordinary):
                path.write_bytes(b"fixture")
            now = 10_000.0
            os.utime(stale, (now - 61, now - 61))
            os.utime(recent, (now - 30, now - 30))
            os.utime(ordinary, (now - 1000, now - 1000))

            cache.prune(cleanup_stale_temps=True, now=now)

            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(ordinary.exists())


if __name__ == "__main__":
    unittest.main()
