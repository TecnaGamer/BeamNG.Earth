"""jobfind: garage-to-garage delivery offers from real garages (the Jobs app)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobfind  # noqa: E402
import mapgeom as mg  # noqa: E402
from test_gasstations import FakeMD, feats  # noqa: E402

LAT0, LON0 = 40.44, -80.00
M = jobfind.M_LAT


def at(dn, de):
    import math
    return LAT0 + dn / M, LON0 + de / (M * math.cos(math.radians(LAT0)))


def place(pid, dn, de, cat="automotive_repair", name="Joe's Auto", **kw):
    a = {"basic_category": cat, "name": name, "operating_status": "open"}
    a.update(kw)
    return (pid, (de, dn), a)


def spot(la, lo):
    return (la, lo, "secondary")


class GarageTest(unittest.TestCase):
    def test_which_places_are_garages(self):
        self.assertTrue(jobfind.is_garage("automotive_repair", ""))
        self.assertTrue(jobfind.is_garage("car_dealer", ""))
        self.assertTrue(jobfind.is_garage("", "automotive > tire_dealer_and_repair"))
        self.assertFalse(jobfind.is_garage("gas_station", ""))
        self.assertFalse(jobfind.is_garage("car_wash", "automotive"))
        self.assertFalse(jobfind.is_garage("restaurant", ""))
        self.assertFalse(jobfind.is_garage("automotive_repair", "", "permanently_closed"))


class OfferTest(unittest.TestCase):
    def setUp(self):
        # garages around the player, and a ring of them 3-25 km away so every drop-off target finds one
        ps = [place("near1", 500.0, 0.0), place("near2", -1500.0, 800.0, cat="car_dealer", name="Smith Motors"),
              place("cafe", 100.0, 100.0, cat="cafe", name="Cafe")]
        import math
        k = 0
        for km in range(3, 26):
            for b in range(0, 360, 15):
                k += 1
                ps.append(place(f"far{k:04d}", km * 1000 * math.cos(math.radians(b)),
                                km * 1000 * math.sin(math.radians(b)), name=f"Garage {k}"))
        self.md = FakeMD({"places": feats("places", ps, mg.POINT)})

    def offers(self, seed=0, route=None):
        return jobfind.offers(self.md, LAT0, LON0, seed, route_fn=route, spot_fn=spot)

    def test_jobs_from_nearby_garages_to_far_ones(self):
        jobs = self.offers()
        self.assertGreaterEqual(len(jobs), 2)
        self.assertLessEqual(len(jobs), jobfind.JOBS)
        self.assertEqual(jobs[0]["pickup"]["name"], "Joe's Auto", "nearest pickup first")
        for j in jobs:
            self.assertNotEqual(j["pickup"]["name"], "Cafe")
            d = jobfind.dist_m(j["pickup"]["lat"], j["pickup"]["lon"], j["dropoff"]["lat"], j["dropoff"]["lon"])
            self.assertGreaterEqual(d, 2000.0)
            self.assertAlmostEqual(j["dist"], d * jobfind.DETOUR, delta=1.0, msg="no route: the estimate")
            self.assertEqual(j["pay"] % 5, 0)
            self.assertGreater(j["limit"], j["dur"])
            self.assertIn(j["car"], [c[0] for c in jobfind.CARS])

    def test_deterministic_and_seeded(self):
        self.assertEqual(self.offers(3), self.offers(3))
        self.assertNotEqual([j["id"] for j in self.offers(3)], [j["id"] for j in self.offers(4)])

    def test_route_used_when_it_answers(self):
        jobs = self.offers(route=lambda *a: {"dist": 16093.44, "dur": 900.0})
        self.assertEqual(jobs[0]["dist"], 16093.4)
        self.assertEqual(jobs[0]["limit"], jobfind.time_limit(900.0))

        def broken(*a):
            raise IOError("offline")
        self.assertEqual(self.offers(route=broken), self.offers())

    def test_pay_is_fair(self):
        # 10 miles in an ordinary car: $60 + $70, + $6 a mile past 4: $36
        self.assertEqual(jobfind.pay_for(10 * jobfind.MI, 1.0), 165.0)
        self.assertEqual(jobfind.pay_for(4 * jobfind.MI, 1.0), 90.0, "the shortest as before")
        self.assertGreater(jobfind.pay_for(10 * jobfind.MI, 2.0), jobfind.pay_for(10 * jobfind.MI, 1.0))
        self.assertEqual(jobfind.time_limit(600.0), 600 * 1.6 + 240)

    def test_no_garages_no_jobs(self):
        md = FakeMD({"places": feats("places", [place("c", 0.0, 0.0, cat="cafe")], mg.POINT)})
        self.assertEqual(jobfind.offers(md, LAT0, LON0, 0, spot_fn=spot), [])


class TruckTest(unittest.TestCase):
    def test_realistic_pay(self):
        # $2.50 a mile, a $350 minimum, the cargo's premium, x1.3 (the user's boost)
        self.assertEqual(jobfind.truck_pay(15 * jobfind.MI, 1.0), 750.0)       # the floor
        self.assertEqual(jobfind.truck_pay(40 * jobfind.MI, 1.25), 1220.0)      # + $9 a mile past 15, tanker
        self.assertEqual(jobfind.truck_pay(55 * jobfind.MI, 1.0), 1110.0)

    def test_freight_between_depots_with_a_rented_semi(self):
        import math
        ps = [place("w1", 600.0, 0.0, cat="warehouse", name="Acme Warehouse"),
              place("g1", 300.0, 0.0, name="Joe's Auto"), place("s1", 200.0, 0.0, cat="self_storage")]
        k = 0
        for km in range(20, 95, 3):
            for b in range(0, 360, 10):
                k += 1
                ps.append(place(f"d{k:04d}", km * 1000 * math.cos(math.radians(b)), km * 1000 * math.sin(math.radians(b)),
                                cat="distribution_center", name=f"DC {k}"))
        md = FakeMD({"places": feats("places", ps, mg.POINT)})
        keep = jobfind.TRUCK_SEARCH_M
        jobfind.TRUCK_SEARCH_M = 6000.0         # (this ring of depots is sparser than a real map's)
        try:
            jobs = jobfind.offers(md, LAT0, LON0, 0, spot_fn=spot, kind="truck")
        finally:
            jobfind.TRUCK_SEARCH_M = keep
        self.assertTrue(jobs)
        self.assertEqual(jobs[0]["pickup"]["name"], "Acme Warehouse", "depots only: not the garage or self storage")
        cargo = {c[0] for c in jobfind.CARGO}
        for j in jobs:
            self.assertEqual((j["kind"], j["car"], j["rent"]), ("truck", "us_semi", jobfind.RENT))
            self.assertIn(j["trailer"], cargo)
            d = jobfind.dist_m(j["pickup"]["lat"], j["pickup"]["lon"], j["dropoff"]["lat"], j["dropoff"]["lon"])
            self.assertGreater(d, 15000.0, "long hauls")
            self.assertGreater(j["pay"], jobfind.RENT * 3, "worth the rent")


if __name__ == "__main__":
    unittest.main()
