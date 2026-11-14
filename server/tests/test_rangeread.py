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

if __name__ == "__main__":
    unittest.main()
