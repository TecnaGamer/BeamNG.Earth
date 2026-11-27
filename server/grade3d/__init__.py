"""The 3D solve of the road and rail network (#100): OSM's 2D layout placed in 3D over the terrain in one convex
problem, the layering of its paths kept, each path held to the laws of its class and speed as costs.

Its steps are functions with typed inputs and outputs, each tested (tests/test_grade3d_<step>.py) and timed
(tools/grade3d_bench.py) on its own:

    network   an area's members -> whole paths, their joins and crossings (the line network)
    layers    crossings -> which path is over, the clearance it needs
    samples   paths -> samples every STEP_M, joins and crossings located on them
    ground    samples -> the smoothed terrain under them
    laws      paths -> comfortable and ceiling grade and curve, the costs of going past them and off the ground
    problem   the above -> the kernel's chains and couplings
    solve     problem -> heights
    outputs   heights -> what the build reads (road plans, decks, tunnels, track beds, fills and cuts)
"""
