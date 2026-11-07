"""Contour unions preserve coverage, gaps and holes without exposing row borders."""
import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from debugcontours import rectangle_rings, features


def area(rings):
    return sum(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(ring,ring[1:]+ring[:1]))/2 for ring in rings)


def contains(rings,x,y):
    hits=0
    for ring in rings:
        for (ax,ay),(bx,by) in zip(ring,ring[1:]+ring[:1]):
            if (ay>y)!=(by>y) and x < ax+(y-ay)*(bx-ax)/(by-ay):
                hits+=1
    return hits%2==1


class DebugContoursTest(unittest.TestCase):
    def test_full_grid_has_only_four_corners(self):
        rings=rectangle_rings([(0,y,40,y+1) for y in range(24)])
        self.assertEqual(len(rings),1)
        self.assertEqual(len(rings[0]),4)
        self.assertEqual(area(rings),960)

    def test_hole_is_not_filled_and_diagonal_patches_stay_separate(self):
        boxes=[(x,y,x+1,y+1) for x in range(3) for y in range(3) if (x,y)!=(1,1)]
        rings=rectangle_rings(boxes)
        self.assertEqual(len(rings),2)
        self.assertEqual(area(rings),8)
        self.assertFalse(contains(rings,1.5,1.5))
        rings=rectangle_rings([(0,0,1,1),(1,1,2,2)])
        self.assertEqual(len(rings),2)
        self.assertEqual(area(rings),2)

    def test_overlap_duplicates_and_partial_edges(self):
        rings=rectangle_rings([(0,0,3,1),(0,0,3,1),(1,0,2,3),(2,1,4,2)])
        self.assertEqual(area(rings),7)
        self.assertFalse(contains(rings,.5,1.5))
        self.assertTrue(contains(rings,3.5,1.5))

    def test_random_cells_retain_exact_membership(self):
        rng=np.random.default_rng(42)
        for _ in range(30):
            mask=rng.random((12,20))>.4
            rings=rectangle_rings([(x,y,x+1,y+1) for y,x in zip(*np.nonzero(mask))])
            self.assertEqual(area(rings),int(mask.sum()))
            for y,x in np.ndindex(mask.shape):
                self.assertEqual(contains(rings,x+.5,y+.5),bool(mask[y,x]))

    def test_categories_empty_and_unwrapped_dateline(self):
        self.assertEqual(features([]),[])
        result=features([[2,179,0,180,1],[2,180,0,181,1],[3,179,1,181,2]],{2:'Arid',3:'Canyon'})
        self.assertEqual([f['type'] for f in result],[2,3])
        self.assertEqual(area(result[0]['rings']),2)
        self.assertTrue(all(f['unwrapped'] for f in result))
        self.assertTrue(all('outline' not in f for f in result))


if __name__=='__main__': unittest.main()
