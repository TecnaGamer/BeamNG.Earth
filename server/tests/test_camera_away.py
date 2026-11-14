"""The 3D map's orbit target is where the drawn world is centred (Connection.camera_away reads cam tx / ty).
The camera itself sits kilometres behind that target, so centring on it would stream the wrong ground."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402


class CameraAwayTest(unittest.TestCase):
    def away(self, cam, focus=(0.0, 0.0)):
        return earthd.Connection.camera_away(SimpleNamespace(focus=focus), cam)

    def test_the_orbit_target_centres_the_view(self):
        self.assertIsNone(
            self.away({"x": 5000.0, "y": 0.0, "tx": 10.0, "ty": 0.0}),
            "target beside the car: the camera behind it does not move the world")
        self.assertEqual(self.away({"x": 5000.0, "y": 0.0, "tx": 200.0, "ty": 0.0}), (200.0, 0.0))
        self.assertEqual(self.away({"x": 200.0, "y": 0.0}), (200.0, 0.0), "a free camera with no target")


if __name__ == "__main__":
    unittest.main()
