"""The 3D solve of the road and rail network (#100, #119): OSM's 2D layout placed in 3D over the terrain. What the map
says is known and held: roads joined in the map meet at one height, a road over another clears it, each road keeps
its grading law. What it does not say, each road's earthwork (its height off the terrain), is the least that holds
them, the faster line moving least.

Its steps are functions with typed inputs and outputs, each tested (tests/test_grade3d_<step>.py) and timed
(tools/grade3d_bench.py) on its own:

    network   an area's members -> whole paths, their joins and crossings (the line network)
    layers    crossings -> which path is over, the clearance it needs
    samples   paths -> samples every STEP_M, joins and crossings located on them
    ground    samples -> the smoothed terrain under them
    laws      paths -> grade and vertical curve, what moving each sample off its terrain costs
    problem   the above -> chains (one per path) and couplings (joins, clearances)
    solve     problem -> heights (a linear program)
    outputs   heights -> what the build reads (road plans, decks, tunnels, track beds, fills and cuts)
"""
