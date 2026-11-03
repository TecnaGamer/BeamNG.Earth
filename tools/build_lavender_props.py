"""Build the procedural 3D Provençal lavender row shape and textures:
  - mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/bne_lavender_b.png
  - mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/bne_lavender_o.png
  - mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/bne_lavender_row.dae

Uses a two-material botanical card architecture (no solid spheres/domes):
  1. `bne_lavender`: BeamNG's native 5-map PBR lavender material
     (`/assets/materials/foliage/groundcover/t_lavender/t_lavender_*`) mapped onto
     8 arched/cupped outward-leaning petal-fan cards per bush clump.
  2. `bne_lavender_foliage`: Custom 512x512 alpha-cutout hemispherical lavender bush clump
     (`bne_lavender_b.png` + `bne_lavender_o.png` explicit opacityMap for PBR v1.5)
     providing a lush silvery-sage woody leaf base and radiating verticillaster flower spikes.
"""
import math
import os
import numpy as np
from PIL import Image

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth"
)


def build_textures(color_path, opacity_path):
    """Generate 512x512 baseColorMap (bne_lavender_b.png) and grayscale opacityMap (bne_lavender_o.png).
    Standard COLLADA UV convention:
      S in [0, 1] -> image column 0 .. W-1
      T in [0, 1] -> image row H-1 (bottom, T=0) .. 0 (top, T=1)
    """
    h, w = 512, 512
    rng = np.random.default_rng(42084)

    # S: left (0) to right (1); T: bottom (0 at row h-1) to top (1 at row 0)
    s = np.linspace(0.0, 1.0, w)[None, :]          # (1, w)
    t = np.linspace(1.0, 0.0, h)[:, None]          # (h, 1): row 0 is T=1.0 (top), row h-1 is T=0.0 (bottom)

    # Multi-scale botanical noise
    n1 = rng.uniform(-1.0, 1.0, (h, w))
    n2 = np.repeat(np.repeat(rng.uniform(-1.0, 1.0, (h // 2, w // 2)), 2, axis=0), 2, axis=1)
    n4 = np.repeat(np.repeat(rng.uniform(-1.0, 1.0, (h // 4, w // 4)), 4, axis=0), 4, axis=1)

    # Background bleed color for transparent texels (prevents dark/bright fringes in mipmaps)
    r = np.full((h, w), 106.0, dtype=np.float64)
    g = np.full((h, w), 86.0, dtype=np.float64)
    b = np.full((h, w), 148.0, dtype=np.float64)
    op = np.zeros((h, w), dtype=np.float64)

    # Polar coordinates from bush root center (S=0.50, T=0.02)
    ds = s - 0.50
    dt = np.maximum(t - 0.01, 0.001)
    rad = np.hypot(ds / 0.46, dt / 0.92)
    ang = np.arctan2(ds, dt)  # -pi/2 .. +pi/2

    # 1) Woody base core & 110 radiating linear silvery-sage leaves (Lavandula angustifolia foliage)
    wood_core = (t >= 0.0) & (rad <= 0.24 + 0.04 * n4) & (np.abs(ds) <= 0.26)
    op = np.where(wood_core, 255.0, op)
    r = np.where(wood_core, 64.0 + 14.0 * n2, r)
    g = np.where(wood_core, 86.0 + 16.0 * n2, g)
    b = np.where(wood_core, 62.0 + 12.0 * n2, b)

    n_leaves = 110
    l_angles = np.linspace(-1.18, 1.18, n_leaves) + rng.uniform(-0.015, 0.015, n_leaves)
    l_tips = rng.uniform(0.30, 0.54, n_leaves) * (1.0 - 0.18 * (l_angles / 1.2) ** 2)
    l_bends = rng.uniform(-0.025, 0.025, n_leaves)
    for la, l_tip, l_bend in zip(l_angles, l_tips, l_bends):
        ca, sa = math.cos(la), math.sin(la)
        dx = s - (0.50 + 0.05 * sa)
        dy = t - 0.02
        u_ray = (dx * sa * 0.46 + dy * ca * 0.92) / max(math.hypot(sa * 0.46, ca * 0.92) ** 2, 1e-4)
        v_perp = (dx * ca - dy * sa) - l_bend * np.clip(u_ray, 0.0, 1.0) ** 1.4
        t_lf = np.clip(u_ray / max(l_tip, 0.1), 0.0, 1.0)
        half_w = 3.2 * np.sin(math.pi * (t_lf ** 0.8))
        in_lf = (u_ray >= 0.02) & (u_ray <= l_tip) & (np.abs(v_perp) * w <= half_w)
        ao_lf = 0.55 + 0.45 * t_lf
        lit_lf = (v_perp > 0.0).astype(np.float64) * 14.0
        op = np.where(in_lf, 255.0, op)
        r = np.where(in_lf, (78.0 + lit_lf + 12.0 * n1) * ao_lf, r)
        g = np.where(in_lf, (108.0 + lit_lf + 14.0 * n1) * ao_lf, g)
        b = np.where(in_lf, (80.0 + 0.8 * lit_lf + 10.0 * n1) * ao_lf, b)

    # 2) 3 overlapping tiers of radiating lavender stalks & whorled flower spikes (78 spikes total)
    #    kept strictly inside S in [0.03, 0.97] so no spike is ever clipped by the card border!
    tiers = [
        # (n_spikes, angle_max, tip_min, tip_max, head_len_min, head_len_max, width_px, shade_mul)
        (26, 0.96, 0.46, 0.66, 0.22, 0.30, 5.6, 0.86),  # Inner/lower tier (fills bush volume densely)
        (28, 0.92, 0.62, 0.82, 0.25, 0.34, 5.2, 0.95),  # Mid crown tier
        (24, 0.86, 0.76, 0.95, 0.26, 0.35, 4.8, 1.04),  # Outer/tallest spike tips
    ]
    for n_sp, a_max, t_min, t_max, hl_min, hl_max, w_px, sh_mul in tiers:
        angles = np.linspace(-a_max, a_max, n_sp) + rng.uniform(-0.016, 0.016, n_sp)
        # Taper radial reach slightly at wide angles so all spikes stay inside S in [0.03, 0.97]
        env = 1.0 - 0.16 * (angles / max(a_max, 0.1)) ** 2
        tip_rads = rng.uniform(t_min, t_max, n_sp) * env
        head_starts = tip_rads - rng.uniform(hl_min, hl_max, n_sp) * env
        bends = rng.uniform(-0.028, 0.028, n_sp)
        phases = rng.uniform(0.0, 2.0 * math.pi, n_sp)

        for a0, r_tip, r_hd0, bend, ph in zip(angles, tip_rads, head_starts, bends, phases):
            ca, sa = math.cos(a0), math.sin(a0)
            s_root = 0.50 + 0.05 * sa
            t_root = 0.03
            dx = s - s_root
            dy = t - t_root
            ray_scale = math.hypot(sa * 0.46, ca * 0.92)
            u_norm = (dx * sa * 0.46 + dy * ca * 0.92) / max(ray_scale * ray_scale, 1e-4)
            curve = bend * np.clip(u_norm, 0.0, 1.2) ** 1.5
            v_perp = (dx * ca - dy * sa) - curve
            dist_px = np.abs(v_perp) * w

            # Slender silvery-green stalk from root up to flower spike
            in_stem = (u_norm >= 0.01) & (u_norm <= r_tip - 0.03) & (dist_px <= 1.5)
            op = np.where(in_stem, 255.0, op)
            r = np.where(in_stem, (84.0 + 12.0 * n1) * sh_mul, r)
            g = np.where(in_stem, (112.0 + 14.0 * n1) * sh_mul, g)
            b = np.where(in_stem, (78.0 + 10.0 * n1) * sh_mul, b)

            # Whorled verticillaster flower spike from r_hd0 to r_tip
            t_sp = np.clip((u_norm - r_hd0) / max(r_tip - r_hd0, 0.08), 0.0, 1.0)
            whorl = 0.74 + 0.32 * np.sin(2.0 * math.pi * t_sp * 6.0 + ph)
            half_w_px = w_px * np.sin(math.pi * (t_sp ** 0.72)) * whorl
            in_head = (u_norm >= r_hd0) & (u_norm <= r_tip) & (dist_px <= half_w_px)

            # Rich Provençal lavender palette: deep violet calyxes + bright lilac-blue sunlit corollas
            side_lit = (v_perp > 0.0).astype(np.float64) * 20.0
            floret = (n1 > 0.20).astype(np.float64) * 26.0
            calyx = (n2 < -0.22).astype(np.float64) * 24.0
            r_hd = (112.0 + side_lit + floret - calyx + 12.0 * n2) * sh_mul
            g_hd = (74.0 + 0.75 * side_lit + 0.80 * floret - 0.75 * calyx + 10.0 * n2) * sh_mul
            b_hd = (186.0 + 1.15 * side_lit + 1.25 * floret - 1.10 * calyx + 16.0 * n2) * sh_mul

            op = np.where(in_head, 255.0, op)
            r = np.where(in_head, r_hd, r)
            g = np.where(in_head, g_hd, g)
            b = np.where(in_head, b_hd, b)

    # Layer 90 feathery silvery-green basal leaves over the lower stalks so the base is completely organic
    for la, l_tip, l_bend in zip(l_angles[:90], l_tips[:90] * 0.78, l_bends[:90]):
        ca, sa = math.cos(la), math.sin(la)
        dx = s - (0.50 + 0.06 * sa)
        dy = t - 0.01
        u_ray = (dx * sa * 0.46 + dy * ca * 0.92) / max(math.hypot(sa * 0.46, ca * 0.92) ** 2, 1e-4)
        v_perp = (dx * ca - dy * sa) - l_bend * np.clip(u_ray, 0.0, 1.0) ** 1.4
        t_lf = np.clip(u_ray / max(l_tip, 0.1), 0.0, 1.0)
        half_w = 2.8 * np.sin(math.pi * (t_lf ** 0.75))
        in_lf = (u_ray >= 0.0) & (u_ray <= l_tip) & (np.abs(v_perp) * w <= half_w)
        ao_lf = 0.52 + 0.48 * t_lf
        lit_lf = (v_perp > 0.0).astype(np.float64) * 16.0
        op = np.where(in_lf, 255.0, op)
        r = np.where(in_lf, (76.0 + lit_lf + 12.0 * n1) * ao_lf, r)
        g = np.where(in_lf, (106.0 + lit_lf + 14.0 * n1) * ao_lf, g)
        b = np.where(in_lf, (78.0 + 0.8 * lit_lf + 10.0 * n1) * ao_lf, b)

    # Guarantee 2px transparent border on left, right, and top edges to prevent UV clamp streaks
    op[:, :2] = 0.0
    op[:, -2:] = 0.0
    op[:2, :] = 0.0

    rgba = np.stack(
        [np.clip(r, 0, 255), np.clip(g, 0, 255), np.clip(b, 0, 255), np.clip(op, 0, 255)],
        axis=-1,
    ).astype(np.uint8)
    Image.fromarray(rgba, "RGBA").save(color_path, optimize=True)
    Image.fromarray(np.clip(op, 0, 255).astype(np.uint8), "L").save(opacity_path, optimize=True)


def build_dae(path):
    """Build bne_lavender_row.dae:
    Two parallel furrows along X (-1.25m .. +1.25m, total length 2.5m so rows follow hillside slopes
    without stair-stepping), spaced 1.36m apart in Y (centres at y = -0.68m and y = +0.68m).
    Each furrow holds 3 overlapping hemispherical lavender bush clumps (at x = -0.82, 0.0, +0.82m).
    Zero solid spheres: every bush is composed of arched/cupped alpha-tested botanical cards
    using both `bne_lavender_foliage` (dense base & spike rosette) and `bne_lavender` (BeamNG's
    native 5-map PBR `/assets/materials/foliage/groundcover/t_lavender/t_lavender_*`).
    """
    positions = []
    normals = []
    uvs = []
    tris_foliage = []
    tris_native = []

    def add_vert(px, py, pz, cx, cy, s_uv, t_uv):
        # Smooth hemispherical outward normal from bush center (cx, cy) so intersecting cards shade uniformly
        dx = px - cx
        dy = py - cy
        dz = max(0.32, pz + 0.15) * 1.15
        ln = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
        idx = len(positions)
        positions.append((px, py, pz))
        normals.append((dx / ln, dy / ln, dz / ln))
        uvs.append((s_uv, t_uv))
        return idx

    def add_arched_card(cx, cy, half_w_bot, half_w_top, z_bot, z_top, z_crest, bow, ang, uv_box, tri_list, lean=0.0):
        """Add a 6-vertex (4-triangle) cupped/arched botanical card centered/leaning at angle `ang`."""
        ca, sa = math.cos(ang), math.sin(ang)
        # Normal/outward direction perpendicular to the card plane: (-sa, ca)
        nx, ny = -sa, ca
        s0, t0, s1, t1 = uv_box
        sm = 0.5 * (s0 + s1)

        # Bottom-left, Bottom-center (bowed), Bottom-right
        r_bot = 0.06 * lean
        v0 = add_vert(cx - half_w_bot * ca + r_bot * nx, cy - half_w_bot * sa + r_bot * ny, z_bot, cx, cy, s0, t0)
        v1 = add_vert(cx + (r_bot + 0.45 * bow) * nx,    cy + (r_bot + 0.45 * bow) * ny,    z_bot, cx, cy, sm, t0)
        v2 = add_vert(cx + half_w_bot * ca + r_bot * nx, cy + half_w_bot * sa + r_bot * ny, z_bot, cx, cy, s1, t0)

        # Top-left, Top-center crest (bowed outward), Top-right
        r_top = lean
        v3 = add_vert(cx - half_w_top * ca + r_top * nx,         cy - half_w_top * sa + r_top * ny,         z_top,   cx, cy, s0, t1)
        v4 = add_vert(cx + (r_top + bow) * nx,                   cy + (r_top + bow) * ny,                   z_crest, cx, cy, sm, t1)
        v5 = add_vert(cx + half_w_top * ca + r_top * nx,         cy + half_w_top * sa + r_top * ny,         z_top,   cx, cy, s1, t1)

        tri_list.append((v0, v1, v4))
        tri_list.append((v0, v4, v3))
        tri_list.append((v1, v2, v5))
        tri_list.append((v1, v5, v4))

    # UV boxes in BeamNG's native t_lavender atlas (S in [0, 1], T in [0, 1] bottom-up)
    native_uvs = [
        (0.02, 0.02, 0.98, 0.41),  # Full 22-spike lavender bouquet
        (0.02, 0.02, 0.50, 0.40),  # Left 10-spike lavender bouquet
        (0.35, 0.02, 0.98, 0.41),  # Right 14-spike lavender bouquet
    ]
    foliage_uv = (0.02, 0.02, 0.98, 0.98)

    bush_xs = [-0.82, 0.0, 0.82]
    for row_idx, y_row in enumerate((-0.68, 0.68)):
        # Continuous along-furrow spine cards connecting the 3 bushes in the row
        for seg_x in (-0.42, 0.42):
            add_arched_card(
                seg_x, y_row,
                half_w_bot=0.62, half_w_top=0.68,
                z_bot=-0.08, z_top=0.58, z_crest=0.64,
                bow=0.10 * (1 if row_idx == 0 else -1),
                ang=0.05 * (1 if seg_x < 0 else -1),
                uv_box=foliage_uv,
                tri_list=tris_foliage,
                lean=0.0,
            )

        for b_idx, bx in enumerate(bush_xs):
            seed = row_idx * 11 + b_idx * 17
            cx = bx + 0.035 * math.sin(seed * 1.9)
            cy = y_row + 0.035 * math.cos(seed * 2.3)
            r_scale = 1.0 + 0.06 * math.sin(seed * 1.3)
            h_scale = 1.0 + 0.07 * math.cos(seed * 2.7)
            phase = (seed * 0.77) % math.pi

            # 1) 5 intersecting full-width hemispherical rosette cards (bne_lavender_foliage)
            for k in range(5):
                ang = phase + k * (math.pi / 5.0)
                sign = 1.0 if (k % 2 == 0) else -1.0
                add_arched_card(
                    cx, cy,
                    half_w_bot=0.46 * r_scale,
                    half_w_top=0.54 * r_scale,
                    z_bot=-0.09,
                    z_top=0.62 * h_scale,
                    z_crest=0.70 * h_scale,
                    bow=0.14 * sign * r_scale,
                    ang=ang,
                    uv_box=foliage_uv,
                    tri_list=tris_foliage,
                    lean=0.05 * sign * r_scale,
                )

            # 2) 8 outward-leaning cupped petal-fan cards around the compass using BeamNG's native t_lavender PBR material
            for k in range(8):
                ang = phase + k * (math.pi / 4.0) + 0.22
                uv_box = native_uvs[(seed + k) % len(native_uvs)]
                is_outer = (k % 2 == 1)
                add_arched_card(
                    cx, cy,
                    half_w_bot=0.28 * r_scale,
                    half_w_top=0.46 * r_scale,
                    z_bot=-0.06 if is_outer else 0.04,
                    z_top=(0.54 if is_outer else 0.68) * h_scale,
                    z_crest=(0.60 if is_outer else 0.74) * h_scale,
                    bow=0.14 * r_scale,
                    ang=ang,
                    uv_box=uv_box,
                    tri_list=tris_native,
                    lean=(0.30 if is_outer else 0.18) * r_scale,
                )

    pos_str = " ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in positions)
    nrm_str = " ".join(f"{nx:.4f} {ny:.4f} {nz:.4f}" for nx, ny, nz in normals)
    uv_str = " ".join(f"{u:.4f} {v:.4f}" for u, v in uvs)
    p_fol_str = " ".join(f"{a} {a} {a} {b} {b} {b} {c} {c} {c}" for a, b, c in tris_foliage)
    p_nat_str = " ".join(f"{a} {a} {a} {b} {b} {b} {c} {c} {c}" for a, b, c in tris_native)

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
<library_effects>
  <effect id="bne_lavender_foliage-fx"><profile_COMMON><technique sid="common"><lambert><diffuse><color>0.52 0.38 0.78 1</color></diffuse></lambert></technique></profile_COMMON></effect>
  <effect id="bne_lavender-fx"><profile_COMMON><technique sid="common"><lambert><diffuse><color>0.55 0.38 0.82 1</color></diffuse></lambert></technique></profile_COMMON></effect>
</library_effects>
<library_materials>
  <material id="bne_lavender_foliage" name="bne_lavender_foliage"><instance_effect url="#bne_lavender_foliage-fx"/></material>
  <material id="bne_lavender" name="bne_lavender"><instance_effect url="#bne_lavender-fx"/></material>
</library_materials>
<library_geometries><geometry id="g0" name="bne_lavender_row"><mesh>
<source id="g0-pos"><float_array id="g0-pos-a" count="{len(positions) * 3}">{pos_str}</float_array>
<technique_common><accessor source="#g0-pos-a" count="{len(positions)}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
<source id="g0-nrm"><float_array id="g0-nrm-a" count="{len(normals) * 3}">{nrm_str}</float_array>
<technique_common><accessor source="#g0-nrm-a" count="{len(normals)}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
<source id="g0-uv"><float_array id="g0-uv-a" count="{len(uvs) * 2}">{uv_str}</float_array>
<technique_common><accessor source="#g0-uv-a" count="{len(uvs)}" stride="2"><param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>
<vertices id="g0-vtx"><input semantic="POSITION" source="#g0-pos"/></vertices>
<triangles material="bne_lavender_foliage" count="{len(tris_foliage)}"><input semantic="VERTEX" source="#g0-vtx" offset="0"/><input semantic="NORMAL" source="#g0-nrm" offset="1"/><input semantic="TEXCOORD" source="#g0-uv" offset="2" set="0"/><p>{p_fol_str}</p></triangles>
<triangles material="bne_lavender" count="{len(tris_native)}"><input semantic="VERTEX" source="#g0-vtx" offset="0"/><input semantic="NORMAL" source="#g0-nrm" offset="1"/><input semantic="TEXCOORD" source="#g0-uv" offset="2" set="0"/><p>{p_nat_str}</p></triangles>
</mesh></geometry></library_geometries>
<library_visual_scenes><visual_scene id="scene"><node id="bne_lavender_row" name="bne_lavender_row"><instance_geometry url="#g0"><bind_material><technique_common>
  <instance_material symbol="bne_lavender_foliage" target="#bne_lavender_foliage"/>
  <instance_material symbol="bne_lavender" target="#bne_lavender"/>
</technique_common></bind_material></instance_geometry></node></visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    color_path = os.path.join(OUT_DIR, "bne_lavender_b.png")
    opacity_path = os.path.join(OUT_DIR, "bne_lavender_o.png")
    dae_path = os.path.join(OUT_DIR, "bne_lavender_row.dae")
    build_textures(color_path, opacity_path)
    build_dae(dae_path)
    print(f"Built {color_path}, {opacity_path}, and {dae_path} ({len(open(dae_path).read())} bytes)")
