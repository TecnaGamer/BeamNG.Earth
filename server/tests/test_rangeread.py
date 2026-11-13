"""Offline regression tests for range-cache races and byte-offset integrity."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netfetch
import rangeread


class Fetcher:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def miss_alive(self, *args):
        return False

    def get(self, url, *, kind, headers):
        start, last = map(int, headers["Range"].removeprefix("bytes=").split("-"))
        end = min(last + 1, len(self.data))
        self.calls.append((start, end))
        return netfetch.Response(206, self.data[start:end],
                                 {"content-range": f"bytes {start}-{end - 1}/{len(self.data)}"})


class RangeIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(rangeread.clear_memory)
        self.data = bytes(range(64))
        self.fetcher = Fetcher(self.data)
        self.remote = rangeread.RemoteFile("https://example.invalid/data", len(self.data),
                                          self.temp.name, self.fetcher, block=16)

    def test_process_finishes_before_lock_acquisition(self):
        """Another process fills block 0 after our second cache miss, before we own its lock."""
        take = rangeread.xproc.take
        first = True

        def completed_before_take(path, kind):
            nonlocal first
            if first:
                first = False
                Path(self.temp.name, "0.blk").write_bytes(self.data[:16])
            return take(path, kind)

        with mock.patch.object(rangeread.xproc, "take", side_effect=completed_before_take):
            self.assertEqual(self.remote.read(0, 48), self.data[:48])
        self.assertEqual(self.fetcher.calls, [(16, 48)])
        self.assertFalse(list(Path(self.temp.name).glob("*.lock")))

    def test_all_blocks_completed_before_lock_need_no_network(self):
        take = rangeread.xproc.take

        def completed_before_take(path, kind):
            index = int(Path(path).name.split(".")[0])
            Path(self.temp.name, f"{index}.blk").write_bytes(self.data[index * 16:(index + 1) * 16])
            return take(path, kind)

        with mock.patch.object(rangeread.xproc, "take", side_effect=completed_before_take):
            self.assertEqual(self.remote.read(0, 48), self.data[:48])
        self.assertEqual(self.fetcher.calls, [])
        self.assertFalse(list(Path(self.temp.name).glob("*.lock")))

    def test_missing_middle_block_is_an_error_not_shifted_bytes(self):
        with mock.patch.object(self.remote, "blocks", return_value={0: self.data[:16], 2: self.data[32:48]}):
            with self.assertRaises(rangeread.RangeError):
                self.remote.read(0, 48)

    def test_missing_last_block_is_an_error_not_short_success(self):
        with mock.patch.object(self.remote, "blocks", return_value={0: self.data[:16]}):
            with self.assertRaises(rangeread.RangeError):
                self.remote.read(0, 32)

    def test_size_discovered_during_read_allows_legitimate_eof(self):
        self.remote.size = None
        self.assertEqual(self.remote.read(48, 80), self.data[48:])
        self.assertEqual(self.remote.size, len(self.data))

    def test_content_range_end_must_match_body(self):
        response = netfetch.Response(206, self.data[:16], {"content-range": "bytes 0-14/64"})
        with mock.patch.object(self.fetcher, "get", return_value=response):
            with self.assertRaises(rangeread.RangeError):
                self.remote.read(0, 16)
        self.assertFalse(list(Path(self.temp.name).glob("*.blk")))

    def test_malformed_content_range_does_not_persist_size(self):
        self.remote.size = None
        response = netfetch.Response(206, self.data[:16], {"content-range": "bytes 0-15/0"})
        with mock.patch.object(self.fetcher, "get", return_value=response):
            with self.assertRaises(rangeread.RangeError):
                self.remote.read(0, 16)
        self.assertIsNone(self.remote.size)
        self.assertFalse(Path(self.temp.name, "size").exists())

    def test_valid_warm_reads_and_eof(self):
        self.assertEqual(self.remote.read(3, 60), self.data[3:60])
        self.assertEqual(self.remote.read(3, 80), self.data[3:])
        self.assertEqual(self.remote.read(80, 100), b"")
        self.assertEqual(len(self.fetcher.calls), 1)



class ParallelRunsTest(unittest.TestCase):
    """A read of more than PAR_BLOCKS blocks of a file of known size goes out as PAR_BLOCKS-block requests at once
    (a far host's single connection can be slow while the line is not); a failed one raises here, counted a fault
    of this thread too."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(rangeread.clear_memory)
        self.data = bytes(i % 251 for i in range(20 * 16))
        self.fetcher = Fetcher(self.data)
        self.remote = rangeread.RemoteFile("https://example.invalid/big", len(self.data), self.temp.name,
                                          self.fetcher, block=16)

    def test_split_and_whole(self):
        got = self.remote.read(0, len(self.data))
        self.assertEqual(got, self.data)
        sizes = sorted(e - s for s, e in self.fetcher.calls)
        self.assertEqual(sizes, sorted([8 * 16, 8 * 16, 4 * 16]), self.fetcher.calls)

    def test_failure_raises_here_and_counts(self):
        import revisit
        real = self.fetcher.get

        def get(url, *, kind, headers):
            if headers["Range"].startswith("bytes=128-"):
                raise netfetch.FetchFailed("broken", url, "example.invalid")
            return real(url, kind=kind, headers=headers)
        self.fetcher.get = get
        before = revisit.thread_faults()
        with self.assertRaises(netfetch.FetchFailed):
            self.remote.read(0, len(self.data))
        self.assertGreater(revisit.thread_faults(), before)


class MappedBlocksTest(unittest.TestCase):
    """The blocks kept in memory are their files' mappings (MAP_MIN): the system's file cache, one copy for every
    server process, where each process held its own 64 MiB of them (67 MB in each of 10, 2026-11-12)."""

    BLOCK = 256 * 1024

    def setUp(self):
        import mmap
        self.mmap = mmap.mmap
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(rangeread.clear_memory)     # (first: a mapped file cannot be removed on Windows)
        rangeread.clear_memory()
        # five full blocks and a short last one (40 KB: under MAP_MIN)
        self.data = bytes((i * 7 + i // 251) % 256 for i in range(5 * self.BLOCK + 40_000))
        self.fetcher = Fetcher(self.data)
        self.remote = rangeread.RemoteFile("https://example.invalid/data", len(self.data), self.temp.name,
                                          self.fetcher)

    def held(self):
        return {k[1]: type(b) for k, b in rangeread._mem.items()}

    def test_fetched_blocks_are_kept_as_their_files_mappings(self):
        self.assertEqual(self.remote.read(10, len(self.data)), self.data[10:])
        self.assertEqual(self.held(), {**{i: self.mmap for i in range(5)}, 5: bytes},
                         "the full blocks mapped, the short last one a copy")
        self.assertEqual(rangeread.mem_mapped(), (5, 5 * self.BLOCK))
        self.assertEqual(rangeread.mem_bytes(), len(self.data))
        # the same bytes from the mappings, any range, and no request for them
        calls = len(self.fetcher.calls)
        for a, b in ((0, 1), (self.BLOCK - 3, self.BLOCK + 5), (3 * self.BLOCK, 4 * self.BLOCK),
                     (123_456, 5 * self.BLOCK + 39_999), (5 * self.BLOCK + 7, len(self.data) + 99)):
            self.assertEqual(self.remote.read(a, b), self.data[a:b], (a, b))
        self.assertEqual(len(self.fetcher.calls), calls)
        self.assertGreater(self.remote.stats["mem_blocks"], 0)

    def test_blocks_on_disk_are_mapped_by_another_process_s_reader(self):
        self.remote.read(0, len(self.data))
        rangeread.clear_memory()            # (a second process: the files, nothing in memory)
        other = rangeread.RemoteFile("https://example.invalid/data", len(self.data), self.temp.name,
                                     Fetcher(b""))
        self.assertEqual(other.read(self.BLOCK - 100, 2 * self.BLOCK + 100),
                         self.data[self.BLOCK - 100:2 * self.BLOCK + 100])
        self.assertEqual(self.held(), {0: self.mmap, 1: self.mmap, 2: self.mmap})
        self.assertEqual(other.stats["disk_blocks"], 3)
        self.assertEqual(other.fetcher.calls, [])

    def test_a_short_block_file_is_not_mapped_but_fetched_again(self):
        self.remote.read(0, 2 * self.BLOCK)
        rangeread.clear_memory()
        with open(os.path.join(self.temp.name, "1.blk"), "r+b") as f:     # (a crash before its data reached the disk)
            f.truncate(self.BLOCK - 1)
        calls = len(self.fetcher.calls)
        self.assertEqual(self.remote.read(0, 2 * self.BLOCK), self.data[:2 * self.BLOCK])
        self.assertEqual(self.fetcher.calls[calls:], [(self.BLOCK, 2 * self.BLOCK)])
        self.assertEqual(os.path.getsize(os.path.join(self.temp.name, "1.blk")), self.BLOCK)

    def test_a_block_written_again_while_mapped_keeps_the_copy_there(self):
        """Windows: a mapped file cannot be replaced; the same bytes are there already (staging.replace_cached)."""
        self.remote.read(0, self.BLOCK)
        with mock.patch.object(rangeread.staging, "REPLACE_WAITS", ()):
            self.remote._store(0, self.data[:self.BLOCK])
        self.assertEqual(self.remote.read(5, 50), self.data[5:50])
        self.assertEqual([n for n in os.listdir(self.temp.name) if n.endswith(".tmp")], [])

    def test_the_lru_drops_its_mappings(self):
        with mock.patch.object(rangeread, "MEM_BYTES", 2 * self.BLOCK):
            self.remote.read(0, 5 * self.BLOCK)
            self.assertEqual(rangeread.mem_mapped(), (2, 2 * self.BLOCK))
            self.assertEqual(sorted(self.held()), [3, 4])
        rangeread.clear_memory()
        self.assertEqual((rangeread.mem_mapped(), rangeread.mem_bytes()), ((0, 0), 0))
        os.remove(os.path.join(self.temp.name, "4.blk"))     # (no longer mapped: it can go, on Windows too)

    def test_copies_with_the_mapping_off(self):
        with mock.patch.object(rangeread, "MAP_MIN", None):
            self.assertEqual(self.remote.read(0, len(self.data)), self.data)
            self.assertEqual(set(self.held().values()), {bytes})
            self.assertEqual(rangeread.mem_mapped(), (0, 0))

    def test_small_blocks_stay_copies(self):
        """The lidar bias's 16 KB blocks: a mapping each would be thousands of descriptors."""
        d = os.path.join(self.temp.name, "small")
        r = rangeread.RemoteFile("https://example.invalid/small", len(self.data), d, self.fetcher, block=16 * 1024)
        self.assertEqual(r.read(1000, 200_000), self.data[1000:200_000])
        self.assertEqual(rangeread.mem_mapped(), (0, 0))


if __name__ == "__main__":
    unittest.main()
