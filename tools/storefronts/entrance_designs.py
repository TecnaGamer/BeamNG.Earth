"""Geometry for shared standalone entrance designs; dimensions are in metres."""
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


def fitted_span(profile, wall_width, entrance, doors):
    """Reserve the gap to the wall ends and halfway to the next door on each side."""
    clear = min(entrance, wall_width - entrance)
    for other in doors:
        if other != entrance:
            clear = min(clear, abs(other - entrance) / 2)
    span = min(profile.span, 1.86 * clear)
    if profile.form == "cinema_lobby":
        return span if span >= 16 and len(set(doors)) == 1 else 0.
    return span if span >= min(6., profile.span*.66) else 0.


def draw(profile, span, height, vertex, mesh, texture, sign):
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
    sign_center = 0.
    if form in {"outfitter", "market", "glass", "step"}:
        sign_h = min(1.90, span * .115)
        sign_width = min(span * .75, sign_h * 7)
    if form == "outfitter":
        rise = half*.5
        prism([(-half,eave),(half,eave),(0,eave+rise)], .02,.18,field)
        for side in (-1,1):
            outline=[(0,eave+rise),(side*half,eave),(side*half,eave+.24),(0,eave+rise+.24)]
            prism(outline, 0, reach, trim)
            post(side*(half-.35),eave+.12,field)
        box(-half,half,reach-.23,reach,eave-.30,eave+.04,field)
        sign_z += .12
    elif form == "drive_in":
        # A wall-attached carhop canopy fits the known building frontage. Leave the
        # centre clear for the entrance and reserve detached rows for mapped site geometry.
        underside = min(3.55, max(2.85, height - .85))
        box(-half, half, .03, reach, underside, underside+.23, field)
        box(-half, half, reach-.11, reach+.04, underside+.23, underside+.33, trim)
        for x in (-half+.25, half-.25):
            post(x, underside, "#a6adb1", .15)
        sign_h = min(1.45, span*.13)
        sign_width = min(span*.58, 3.25*sign_h)
        sign_z, sign_depth = underside+.33+sign_h/2, reach+.045
        box(-sign_width/2, sign_width/2, reach-.12, reach+.025,
            sign_z-sign_h/2, sign_z+sign_h/2, field)
    elif form == "hip":
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
        sign_z = (eave+top)/2
        box(-sign_width*.51,sign_width*.51,inner,reach,sign_z-sign_h*.52,sign_z+sign_h*.52,trim)
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
    elif form in {"shed_lobby","glazed_shed"}:
        # Timber rainscreen above a glazed vestibule under an asymmetric shed roof.
        sign_depth = .65
        overhang = reach - sign_depth
        low = max(height + .18, 4.05 + .45 * overhang)
        high = low + min(2.15, span * .13) - .18
        panel = "#526a72" if form == "glazed_shed" else field
        prism([(-half,3.1),(half,3.1),(half,high),(-half,low)],.04,.58,panel)
        if form == "shed_lobby":
            for z in np.arange(3.38,low,.34):
                box(-half,half,.58,.60,z,z+.033,"#4c392f")
        else:
            for x in np.linspace(-half,half,9):
                top = low+(high-low)*(x+half)/span
                box(x-.04,x+.04,.58,.62,3.1,top,trim)
            box(-half,half,.58,.62,4.05,4.14,trim)
        prism([(-half,low),(half,high),(half,high+.27),(-half,low+.27)],.02,reach,trim)
        for x in (-half+.22,half-.22):
            post(x,low if x < 0 else high,trim,.25)
        sign_h = min(2.2,span*.13)
        sign_width = min(span*.72,sign_h*5.4)
        # Fit the whole sign between the lobby opening and the projecting roof.
        # Reserve the sightline over the low end, including an oblique approach.
        roof_x = -sign_width / 2 - overhang
        roof_at_edge = low + (high - low) * max(0., roof_x + half) / span
        clear_top = roof_at_edge - .45 * overhang - .15
        clear_bottom = 3.22
        sign_h = min(sign_h, clear_top - clear_bottom)
        sign_z = (clear_top + clear_bottom) / 2
    elif form == "masonry_hall":
        spring = height-.45
        ridge = spring + min(3.4,span*.16)
        prism([(-half,spring),(half,spring),(0,ridge)],.02,reach,field)
        for edge in (-half,half):
            prism([(edge,spring),(0,ridge),(0,ridge+.25),(edge,spring+.25)],.02,reach+.12,trim)
            pier = edge*.87
            box(pier-.38,pier+.38,.02,reach,0,spring,field)
            box(pier-.49,pier+.49,.02,reach+.08,0,1.15,"#b4a793")
        sign_h = min(2.,span*.10)
        sign_width = min(span*.65,sign_h*4.5)
        sign_z,sign_depth = spring+.35,reach+.16
        box(-sign_width*.52,sign_width*.52,.02,reach+.13,sign_z-sign_h*.54,sign_z+sign_h*.54,field)
    elif form == "timber_portal":
        sign_h = min(2.2,span*.13)
        sign_width = min(span*.81,sign_h*5.6)
        sign_z,sign_depth = max(height,3.9+sign_h*.5),reach+.04
        top = sign_z+sign_h*.64
        box(-half,half,.04,reach,sign_z-sign_h*.6,top,field)
        for edge in (-half+.32,half-.32):
            box(edge-.30,edge+.30,.04,reach,0,top,field)
        box(-half,half,.03,reach+.18,top,top+.14,trim)
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
        sign_width = min(span*.62,sign_h*5.2)
        sign_z,sign_depth = spring+rise*.30,.97
    elif form == "streamline":
        # Stepped horizontal fins with clipped ends frame the diner entrance.
        for i in range(3):
            h=half*(1-i*.075)
            z=3.05+i*.34
            prism([(-h+.25,z),(h-.25,z),(h,z+.12),(h-.18,z+.23),(-h+.18,z+.23),(-h,z+.12)],
                  .03,reach*(1-i*.16),trim if i==1 else field)
        sign_z,sign_depth=max(height+.04, 4.05+sign_h/2),.23
        box(-sign_width*.55,sign_width*.55,.02,.2,sign_z-sign_h*.6,sign_z+sign_h*.6,trim)
    elif form == "step":
        for i in range(3):
            h=half*(1-i*.20)
            z=eave+i*.62
            box(-h,h,.03,reach-i*.15,z-.38,z+.40,field)
            box(-h,h,.03,reach-i*.15+.04,z+.28,z+.40,trim)
        sign_z,sign_depth=eave+.67,reach+.05
    elif form in {"glass","grid_showroom","screen_showroom"}:
        sign_h = min(2.25,span*.17)
        sign_width = min(span*.78,sign_h*5)
        high = max(height+.3,3.65+sign_h)
        low = high-sign_h-.30
        box(-half,half,.02,reach,low,high,field)
        sign_z,sign_depth = (low+high)/2,reach+.025
        for x in (-half+.17,half-.17):
            post(x,low,trim,.28)
        if form == "screen_showroom":
            # Space the fins outside the badge so they never cross the lettering.
            for x in np.arange(-half+.2,half-.12,.42):
                if abs(x) > sign_width/2+.2:
                    box(x-.06,x+.06,reach,reach+.13,low,high,trim)
        elif form == "grid_showroom":
            for side in (-1,1):
                for x in np.arange(3.5,half-.45,2.2):
                    box(side*x-.05,side*x+.05,.02,.24,.15,low,trim)
            box(-half,half,.02,.25,2.62,2.72,trim)
    elif form == "market":
        high = height+.95
        for x in (-half+.25,half-.25):
            post(x,high,trim,.45)
        box(-half,half,.02,reach,high-.39,high,field)
        ridge=high+min(2.0,span*.12)
        prism([(-half,high),(half,high),(0,ridge)],.03,reach-.08,"#708987")
        for side in (-1,1):
            prism([(0,ridge),(side*half,high),(side*half,high+.15),(0,ridge+.15)],.03,reach,trim)
        sign_z=high+.32

    x=sign_width/2
    lo,hi=sign_z-sign_h/2,sign_z+sign_h/2
    corners=[vertex(sign_center-x,sign_depth,lo),vertex(sign_center+x,sign_depth,lo),
             vertex(sign_center+x,sign_depth,hi),vertex(sign_center-x,sign_depth,hi)]
    mesh([corners[i] for i in (0,1,2,0,2,3)], texture(sign(sign_width/sign_h, sign_h)),
         [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
