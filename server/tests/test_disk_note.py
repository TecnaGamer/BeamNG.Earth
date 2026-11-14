"""The status text's note for a drive short of its free reserve (earthd.Server.net_note + cache.Cache.disk_short): the
first week's most frequent errors were "No space left on device"."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import earthd  # noqa: E402


class Net:
    offline = False

    def unavailable_hosts(self, optional=False):
        return []

    def slow(self):
        return None


class Disk:
    def __init__(self, short, managed=True):
        self.short, self.managed = short, managed

    def disk_short(self):
        return self.short


class DiskNoteTest(unittest.TestCase):
    def server(self, disk):
        s = types.SimpleNamespace(net=Net(), degraded=set())
        if disk is not None:
            s.disk_cache = disk
        return s

    def test_note_only_when_short(self):
        self.assertEqual(earthd.Server.net_note(self.server(None)), "")
        self.assertEqual(earthd.Server.net_note(self.server(Disk(0))), "")
        self.assertIn("drive is nearly full", earthd.Server.net_note(self.server(Disk(5 * 10 ** 8))))
        self.assertEqual(earthd.Server.net_note(self.server(Disk(5 * 10 ** 8, managed=False))), "",
                         "a cache left alone (a newer layout) says nothing")


if __name__ == "__main__":
    unittest.main()
