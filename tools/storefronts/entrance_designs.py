"""Geometry for recognized standalone entrance designs; dimensions are in metres."""
from dataclasses import dataclass
from math import cos, pi, sin

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class Entrance:
    form: str
    finish: str
    trim: str
    span: float


# These are representative built formats. A named business inside another building uses a tenant panel.
DESIGNS = {
    "Whataburger": Entrance("striped_gable", "#d76621", "#fff1d9", 13),
    "IHOP": Entrance("striped_gable", "#235388", "#235388", 10),
    "Pizza Hut": Entrance("hat_roof", "#a4352c", "#542b25", 19),
    "In-N-Out Burger": Entrance("tile_canopy", "#ad563c", "#c92e2b", 22),
    "Chili's": Entrance("awning_tower", "#b2987e", "#315d47", 21),
    "ALDI": Entrance("shed_lobby", "#775540", "#354951", 22),
    "Panda Express": Entrance("hip", "#a6322a", "#3b3732", 14),
    "Bob Evans": Entrance("hip", "#992f2c", "#f2e2c3", 21),
    "Perkins": Entrance("hip", "#426849", "#e7dfc5", 18),
    "White Castle": Entrance("castle", "#e7e5df", "#305d91", 14),
    "Olive Garden": Entrance("arcade", "#c9ad88", "#8f4832", 15),
    "Taco Cabana": Entrance("arcade", "#d4a386", "#a44c38", 13),
    "El Pollo Loco": Entrance("arcade", "#d4af81", "#a74d32", 12),
    "Taco Bueno": Entrance("arcade", "#d0a383", "#8b4730", 11),
    "Pollo Tropical": Entrance("arcade", "#e8c79c", "#a86743", 15),
    "Texas Roadhouse": Entrance("porch", "#68503b", "#486447", 18),
    "LongHorn Steakhouse": Entrance("porch", "#785641", "#53423c", 15),
    "Outback Steakhouse": Entrance("porch", "#876545", "#775446", 16),
    "Zaxby's": Entrance("porch", "#e0cbb1", "#80473d", 12),
    "Biscuitville": Entrance("porch", "#c4b095", "#584e48", 10),
    "Cabela's": Entrance("outfitter", "#6a503c", "#425849", 23),
    "REI": Entrance("outfitter", "#755a42", "#394d46", 15),
    "Tractor Supply Co.": Entrance("outfitter", "#796553", "#88423a", 17),
    "Menards": Entrance("outfitter", "#8d816a", "#38684d", 24),
    "Sprouts Farmers Market": Entrance("market", "#49673e", "#c7b28a", 22),
    "The Fresh Market": Entrance("market", "#435343", "#ab9b78", 17),
    "Fresh Thyme Market": Entrance("market", "#637b4d", "#b09972", 18),
    "Whole Foods Market": Entrance("market", "#345c49", "#897559", 20),
    "Wegmans": Entrance("market", "#726049", "#9d8564", 24),
    "Dutch Bros Coffee": Entrance("wing", "#23578a", "#d9e6e6", 9),
    "Scooter's Coffee": Entrance("wing", "#aa3635", "#483d37", 8),
    "Biggby Coffee": Entrance("wing", "#c46a31", "#433d36", 9),
    "Checkers": Entrance("streamline", "#b63230", "#dee3df", 13),
    "Rally's": Entrance("streamline", "#b63230", "#dee3df", 13),
    "Steak 'n Shake": Entrance("streamline", "#302f30", "#d84a3b", 18),
    "Freddy's Frozen Custard & Steakburgers": Entrance("streamline", "#b63232", "#e9ddcc", 16),
    "Krispy Kreme": Entrance("streamline", "#41674e", "#f0e5d4", 17),
    "AutoZone": Entrance("step", "#c2442d", "#e8a343", 18),
    "Advance Auto Parts": Entrance("step", "#a62e2d", "#e7bb40", 16),
    "O'Reilly Auto Parts": Entrance("step", "#276442", "#dfd9bd", 18),
    "Harbor Freight Tools": Entrance("step", "#9e302c", "#494744", 21),
    "AMC Theatres": Entrance("cinema_lobby", "#bda78c", "#932a36", 32),
    "Apple Store": Entrance("glass", "#bdbfbb", "#626962", 23),
    "Crate & Barrel": Entrance("glass", "#ddd8cb", "#494a46", 20),
    "Lululemon": Entrance("glass", "#bdb6a9", "#52504a", 17),
}


def fitted_span(name, wall_width, entrance, doors):
    """Reserve the gap to the wall ends and halfway to the next door on each side."""
    profile = DESIGNS.get(name)
    if profile is None:
        return 0.
    clear = min(entrance, wall_width - entrance)
    for other in doors:
        if other != entrance:
            clear = min(clear, abs(other - entrance) / 2)
    span = min(profile.span, 1.86 * clear)
    if profile.form == "cinema_lobby":
        return span if span >= 16 and len(set(doors)) == 1 else 0.
    return span if span >= min(6., profile.span*.66) else 0.


def draw(profile, span, height, vertex, mesh, texture, sign, symbol_only=False):
    """Draw a fitted entrance around x=0. Positive depth faces out from the building."""
    half = span / 2
    reach = min(2.6, span*.13)
    colors = {}

    def material(color):
        if color not in colors:
            colors[color] = texture(Image.new("RGB", (4, 4), color))
        return colors[color]

    def surface(points, paint):
        # Every polygon supplied here is convex and coplanar.
        triangles = [j for i in range(1, len(points)-1) for j in (0,i,i+1)]
        coords = [vertex(*points[j]) for j in triangles]
        mesh(coords, material(paint), [(0,0)] * len(coords))

    def prism(outline, near, far, paint):
        # Extrude a front elevation in depth. The two end faces and all returns are closed.
        front = [(x,far,z) for x,z in outline]
        back = [(x,near,z) for x,z in outline]
        surface(front, paint)
        surface(back[::-1], paint)
        for i in range(len(front)):
            j = (i+1) % len(front)
            surface([back[i], back[j], front[j], front[i]], paint)

    def box(x0, x1, near, far, low, high, paint):
        prism([(x0,low),(x1,low),(x1,high),(x0,high)], near, far, paint)

    def post(x, top, paint, breadth=.34):
        box(x-breadth/2, x+breadth/2, reach-breadth, reach, 0, top, paint)

    form, field, trim = profile.form, profile.finish, profile.trim
    eave = max(3.25, height-.45)
    sign_z, sign_depth, sign_h = eave, reach+.04, min(1.15, span*.095)
    sign_width = min(span*.69, sign_h*6)
    if form in {"outfitter", "market", "glass", "step"}:
        sign_h = min(1.90, span * .115)
        sign_width = min(span * .75, sign_h * 7)
    if form in {"striped_gable", "outfitter"}:
        rise = half*(.82 if form == "striped_gable" else .50)
        prism([(-half,eave),(half,eave),(0,eave+rise)], .02,.18,
              "#5c7479" if form=="striped_gable" else field)
        if form=="striped_gable":
            for v in (-.6,-.3,0.,.3,.6):
                x = v*half
                box(x-.075,x+.075,.18,.25,eave,eave+rise*(1-abs(v)),trim)
        for side in (-1,1):
            outline=[(0,eave+rise),(side*half,eave),(side*half,eave+.24),(0,eave+rise+.24)]
            for i in range(6):
                prism(outline, reach*i/6, reach*(i+1)/6, trim if i%2 or form=="outfitter" else field)
            post(side*(half-.35),eave+.12,field)
        box(-half,half,reach-.23,reach,eave-.30,eave+.04,field)
        sign_z += .12
    elif form in {"hip", "hat_roof"}:
        top = eave + min(2.6, half*.37)
        upper = .62*half
        outer = reach+.18
        inner = reach*.35
        surface([(-half,outer,eave),(half,outer,eave),(upper,inner,top),(-upper,inner,top)],field)
        for side in (-1,1):
            surface([(side*half,0,eave),(side*half,outer,eave),
                     (side*upper,inner,top),(side*upper,0,top)],field)
        surface([(-upper,0,top),(upper,0,top),(upper,inner,top),(-upper,inner,top)],trim)
        prism([(-half,eave),(half,eave),(upper,top),(-upper,top)],0,.03,field)
        box(-half,half,.03,reach+.15,eave-.24,eave,trim)
        box(-upper,upper,0,inner+.12,top,top+.17,trim)
        if form == "hat_roof":
            # The raised cap and broad eaves give the classic red roof its two slopes.
            cap = upper*.67
            ridge = top + min(1.5,half*.18)
            surface([(-upper,inner+.12,top+.17),(upper,inner+.12,top+.17),
                     (cap,.04,ridge),(-cap,.04,ridge)],field)
            for side in (-1,1):
                surface([(side*upper,.02,top+.17),(side*upper,inner+.12,top+.17),
                         (side*cap,.04,ridge)],field)
            box(-cap,cap,.02,.21,ridge,ridge+.14,field)
        sign_z = (eave+top)/2
        box(-sign_width*.51,sign_width*.51,inner,reach,sign_z-sign_h*.52,sign_z+sign_h*.52,trim)
    elif form == "castle":
        box(-half,half,.03,reach,eave-.55,eave+.45,field)
        for side in (-1,1):
            x=side*half*.76
            box(x-.17*half,x+.17*half,.03,reach+.17,0,eave+1.10,field)
            box(x-.18*half,x+.18*half,.03,reach+.23,eave+.75,eave+.90,trim)
            for dx in (-.12*half,.12*half):
                box(x+dx-.21,x+dx+.21,.03,reach+.17,eave+1.1,eave+1.56,field)
        sign_width *= .80
    elif form == "arcade":
        # Three real openings; keep the centre opening aligned with the customer door.
        radius = span/6
        spring = 2.55
        arch_height=min(1.65, radius*.58)
        top=max(height+.45,spring+arch_height+.35)
        for x in (-2*radius,0.,2*radius):
            for a,b in zip(np.linspace(0,pi,9)[:-1],np.linspace(0,pi,9)[1:]):
                u,v=x+radius*cos(a),x+radius*cos(b)
                prism([(u,spring+arch_height*sin(a)),(v,spring+arch_height*sin(b)),(v,top),(u,top)],reach-.26,reach,field)
        for x in (-half,-radius,radius,half):
            post(x*.97,spring+.08,field)
        box(-half,half,.02,reach+.12,top-.12,top+.69,trim)
        sign_z,sign_depth=top+.26,reach+.18
        sign_h=min(sign_h,.72)
    elif form in {"porch","wing"}:
        left_z,right_z=(3.15,3.15) if form=="porch" else (3.25,4.15)
        back_z=4.15 if form=="porch" else 3.7
        # A shed roof drops toward the posts; the coffee canopy climbs at one end.
        for offset in (0.,.17):
            surface([(-half,0,back_z+offset),(half,0,back_z+right_z-left_z+offset),
                     (half,reach,right_z+offset),(-half,reach,left_z+offset)],trim)
        prism([(-half,left_z),(half,right_z),(half,right_z+.19),(-half,left_z+.19)],reach-.16,reach,field)
        for x,z in ((-half+.25,left_z),(half-.25,right_z)):
            post(x,z,field,.32 if form=="porch" else .20)
        sign_z=(left_z+right_z)/2+.48
        sign_depth=reach+.08
        box(-sign_width*.53,sign_width*.53,reach-.20,reach+.04,
            sign_z-sign_h*.55,sign_z+sign_h*.55,field)
    elif form in {"tile_canopy", "awning_tower"}:
        tiled = form == "tile_canopy"
        tower = span * .18
        crown = height + (1.1 if tiled else 1.7)
        box(-tower,tower,.04,.85,2.95,crown,"#f1e8d6" if tiled else field)
        if tiled:
            for left,right in ((-half,-tower),(tower,half)):
                surface([(left,reach,eave),(right,reach,eave),(right,.02,eave+.83),
                         (left,.02,eave+.83)],field)
                box(left,right,reach-.1,reach,eave-.16,eave,"#f1e8d6")
        else:
            box(-tower-.15,tower+.15,.03,1.02,crown-.22,crown+.08,trim)
        for left,right in ((-half,-tower-.22),(tower+.22,half)):
            # Each fabric awning shades a dining window and leaves the entry clear.
            panels = 1 if tiled else max(3,round((right-left)/.46))
            for j in range(panels):
                x = left+(right-left)*j/panels
                u = left+(right-left)*(j+1)/panels
                color = trim if j%2 == 0 else "#e7dbb9"
                surface([(x,1.28,2.75),(u,1.28,2.75),(u,.08,3.30),(x,.08,3.30)],color)
                surface([(x,1.28,2.57),(u,1.28,2.57),(u,1.28,2.75),(x,1.28,2.75)],color)
        sign_h = min(2.60 if tiled else 1.55,tower*.80)
        sign_width = tower*1.85
        sign_z,sign_depth = crown-sign_h*.60,.90
    elif form == "shed_lobby":
        # Timber rainscreen above a glazed vestibule under an asymmetric shed roof.
        low, high = height+.18, height+min(2.15,span*.13)
        prism([(-half,3.1),(half,3.1),(half,high),(-half,low)],.04,.58,field)
        for z in np.arange(3.38,low,.34):
            box(-half,half,.58,.60,z,z+.033,"#4c392f")
        prism([(-half,low),(half,high),(half,high+.27),(-half,low+.27)],.02,reach,trim)
        for x in (-half+.22,half-.22):
            post(x,low if x < 0 else high,trim,.25)
        sign_h = min(2.2,span*.13)
        sign_width = min(half,sign_h*2.6)
        sign_z,sign_depth = (high+low)*.5-sign_h*.57,.65
    elif form == "cinema_lobby":
        # A high arched clerestory breaks up the flat roofline of the auditorium block.
        spring = max(5.7, height + .35)
        radius = half * .72
        rise = min(4.2, span * .14)
        points = [(radius*cos(a), spring+rise*sin(a)) for a in np.linspace(pi, 0, 13)]
        prism([(-radius, 3.3), (radius, 3.3), *reversed(points)], .04, .68, "#41606b")
        for (x,z),(u,v) in zip(points, points[1:]):
            prism([(x,z),(u,v),(u,v+.40),(x,z+.40)], .03, .92, field)
            if abs(x) < radius-.01:
                box(x-.055,x+.055,.68,.75,3.4,z,"#b3b6ad")
        box(-radius,radius,.68,.76,spring-.14,spring,"#b3b6ad")
        for side in (-1,1):
            x = side * (radius + .53)
            box(x-.62,x+.62,.05,1.45,0,spring+.55,field)
            box(x-.37,x+.37,1.45,1.53,.45,spring+.24,trim)
            box(x-.79,x+.79,.04,1.62,spring+.45,spring+.77,field)
        # Chamfered corners make the marquee visible on an oblique approach.
        reach = min(4.5, span*.16)
        outline = [(-half,.06),(half,.06),(half,reach*.42),
                   (half-1.5,reach),(-half+1.5,reach),(-half,reach*.42)]
        for low,high,color in ((3.25,3.41,trim),(3.41,4.13,"#ead8aa"),(4.13,4.34,trim)):
            surface([(x,y,high) for x,y in outline],color)
            surface([(x,y,low) for x,y in reversed(outline)],color)
            for (x,y),(u,v) in zip(outline,outline[1:]+outline[:1]):
                surface([(x,y,low),(u,v,low),(u,v,high),(x,y,high)],color)
        # Repeated pairs of glazed lobby doors sit under the marquee.
        for x in np.linspace(-radius*.72,radius*.72,5):
            box(x-1.05,x+1.05,.03,.18,.06,2.78,"#c3b9a5")
            for dx in (-.49,.49):
                box(x+dx-.43,x+dx+.43,.18,.20,.17,2.65,"#354b50")
                box(x+dx-.40,x+dx+.40,.20,.22,1.03,1.11,"#c3b9a5")
        # Abstract poster art gives the entrance cases a readable silhouette at road distance.
        for side,sky,land in ((-1,"#406378","#efbd70"),(1,"#734052","#c1c5ab")):
            x = side * (half-.95)
            box(x-.73,x+.73,.03,.20,.57,2.65,"#352e32")
            surface([(x-.62,.21,.71),(x+.62,.21,.71),(x+.62,.21,2.51),(x-.62,.21,2.51)],sky)
            surface([(x-.62,.22,.71),(x+.62,.22,.71),(x+.62,.22,1.95),(x-.62,.22,1.12)],land)
            box(x-.41,x+.41,.22,.23,.84,.92,"#eee2cb")
        sign_h = min(2.55, span*.095)
        sign_width = sign_h*2.8
        sign_z,sign_depth = spring+rise*.30,.97
    elif form == "streamline":
        # Stepped horizontal fins with clipped ends frame the diner entrance.
        for i in range(3):
            h=half*(1-i*.075)
            z=3.05+i*.34
            prism([(-h+.25,z),(h-.25,z),(h,z+.12),(h-.18,z+.23),(-h+.18,z+.23),(-h,z+.12)],
                  .03,reach*(1-i*.16),trim if i==1 else field)
        sign_z,sign_depth=height+.04,.23
        box(-sign_width*.55,sign_width*.55,.02,.2,sign_z-sign_h*.6,sign_z+sign_h*.6,trim)
    elif form == "step":
        for i in range(3):
            h=half*(1-i*.20)
            z=eave+i*.62
            box(-h,h,.03,reach-i*.15,z-.38,z+.40,field)
            box(-h,h,.03,reach-i*.15+.04,z+.28,z+.40,trim)
        sign_z,sign_depth=eave+.67,reach+.05
    else:  # Market hall or glazed showroom: glazing is painted onto this wall behind the frame.
        high=height+(.95 if form=="market" else .35)
        for x in (-half+.25,half-.25):
            post(x,high,trim,.45)
        box(-half,half,.02,reach,high-.39,high,field)
        if form=="market":
            ridge=high+min(2.0,span*.12)
            prism([(-half,high),(half,high),(0,ridge)],.03,reach-.08,"#708987")
            for side in (-1,1):
                prism([(0,ridge),(side*half,high),(side*half,high+.15),(0,ridge+.15)],.03,reach,trim)
            sign_z=high+.32
        else:
            box(-half,half,.02,reach,3.38,3.54,trim)
            sign_z,sign_depth=high-.70,.14

    if symbol_only:
        sign_h = min(2.1, height*.33, span*.31)
        sign_width = sign_h
        sign_z = height - sign_h*.60
    x=sign_width/2
    lo,hi=sign_z-sign_h/2,sign_z+sign_h/2
    corners=[vertex(-x,sign_depth,lo),vertex(x,sign_depth,lo),vertex(x,sign_depth,hi),vertex(-x,sign_depth,hi)]
    mesh([corners[i] for i in (0,1,2,0,2,3)], texture(sign(sign_width/sign_h, sign_h)),
         [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
