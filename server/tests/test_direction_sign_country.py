"""Direction board design follows its own coordinates, including border roads."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unittest
from unittest.mock import Mock, patch
import dirsigns
import geodesy

class BoardCountryTest(unittest.TestCase):
    def test_board_on_french_road_uses_france_even_for_foreign_destinations(self):
        frame = geodesy.EnuFrame(48.82, 7.78)
        border = Mock()
        border.at.return_value = 'FR'
        with patch('countrybounds.bundled', return_value=border):
            cc = dirsigns.board_country(frame, 250., -130.)
        lat, lon, _ = frame.enu_to_geodetic(250., -130., 0.)
        border.at.assert_called_once_with(float(lat), float(lon))
        self.assertEqual(cc, 'FR')
        # This generic generator supports UA/CZ/PL/DE, not France: it must skip
        # the board rather than borrow a German destination's design.
        self.assertIsNone(dirsigns.styles()['countries'].get(cc))

    def test_two_boards_of_one_border_tile_can_use_different_countries(self):
        frame = geodesy.EnuFrame(48.82, 7.78)
        border = Mock()
        border.at.side_effect = ['FR', 'DE', None]
        with patch('countrybounds.bundled', return_value=border):
            self.assertEqual(dirsigns.board_country(frame, 0., 0.), 'FR')
            self.assertEqual(dirsigns.board_country(frame, 250., 0.), 'DE')
            # No destination nationality is substituted when the border data is absent.
            self.assertIsNone(dirsigns.board_country(frame, 500., 0.))
