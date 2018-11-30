import unittest
import copy

import astropy.units as q

from .. import isochrone as iso


class TestPARSEC(unittest.TestCase):
    """Tests for the PARSEC model isochrones"""
    def setUp(self):
        # Make Spectrum class for testing
        self.hsa = iso.Isochrone('hybrid_solar_age')

    def test_interp(self):
        """Test that the model isochrone can be interpolated"""
        pass
