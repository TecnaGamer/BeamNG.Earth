"""Build GitHub catalog pages from actual standalone exports on sample footprints."""
import io
import tempfile
from itertools import groupby
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from build_reference import ROOT, STYLE_NAMES
from build_stores import export_store
from storeidentity import CATEGORY_RULES, CHAIN_GROUPS, SOURCES, STYLES, identity_for
from whole_store import SIGN_FONT, sign_font_scope
from threedmr import gltf_mesh


def render_mesh(mesh, size=(640, 248)):
    positions, uv, triangles, _, materials, slots = mesh
    camera = np.array([0.43, -1.0, 0.27])
    camera /= np.linalg.norm(camera)
    right = np.cross([0, 0, 1], camera)
    right /= np.linalg.norm(right)
    up = np.cross(camera, right)
    screen = np.column_stack([positions @ right, -positions @ up])
    screen -= screen.min(axis=0)
    screen *= min((np.array(size) - 24) / screen.max(axis=0))
    screen += (np.array(size) - screen.max(axis=0)) / 2
    depth = positions @ camera
    zbuffer = np.full(size[::-1], -np.inf)
    image = np.full((*size[::-1], 3), [239, 242, 238], dtype=np.uint8)
    textures = [np.asarray(Image.open(io.BytesIO(m[0])).convert("RGB")) if m[0] else None for m in materials]
    for indices, slot in zip(triangles, slots):
        points = screen[indices]
        x, y = np.floor(points.min(axis=0)).astype(int)
        xmax, ymax = np.ceil(points.max(axis=0)).astype(int)
        a, b, c = points
        ab, ac = b - a, c - a
        determinant = ab[0] * ac[1] - ab[1] * ac[0]
        if abs(determinant) < 1e-7:
            continue
        yy, xx = np.mgrid[y:ymax + 1, x:xmax + 1]
        dx, dy = xx + 0.5 - a[0], yy + 0.5 - a[1]
        wb = (dx * ac[1] - dy * ac[0]) / determinant
        wc = (ab[0] * dy - ab[1] * dx) / determinant
        weights = np.stack((1 - wb - wc, wb, wc), axis=-1)
        z = weights @ depth[indices]
        previous = zbuffer[y:ymax + 1, x:xmax + 1]
        visible = (weights.min(axis=-1) >= -1e-7) & (z >= previous - 1e-10)
        if not visible.any():
            continue
        texture = textures[slot]
        factor = materials[slot][2]
        color = np.array(factor if factor is not None else [1, 1, 1])[:3]
        if texture is None:
            color = color * 255
        else:
            u, v = (weights[visible] @ uv[indices]).T
            height, width = texture.shape[:2]
            texels = texture[np.clip(((1 - v) * height).astype(int), 0, height - 1),
                             np.clip((u * width).astype(int), 0, width - 1)]
            color = color * texels
        normal = np.cross(positions[indices[1]] - positions[indices[0]],
                          positions[indices[2]] - positions[indices[0]])
        shade = 0.85 + 0.15 * abs(normal @ [-0.35, -0.72, 0.59]) / max(1e-12, np.linalg.norm(normal))
        image[y:ymax + 1, x:xmax + 1][visible] = np.clip(color * shade, 0, 255)
        previous[visible] = z[visible]
    return Image.fromarray(image)


def sample(source, identity, directory, *, category=None):
    width = max(23, 2.5 * STYLES[identity.style]["span"])
    depth = width * 0.66
    half = np.array([width / 2, depth / 2]) / [111320, 111132]
    footprint = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) * half
    door = np.array([0, -half[1]])
    match = (source, identity.key, footprint, np.array([0., -1.]), [(door, False)], None)
    entry = export_store(Path("sample.osm"), match, {}, directory, category=category)
    if entry["fictional_identity"] != identity.key:
        raise ValueError(f"Export for {source} used {entry['fictional_identity']}")
    return render_mesh(gltf_mesh((directory / entry["file"]).read_bytes())), entry["storefront_variant"]


@sign_font_scope()
def build():
    destination = ROOT / "catalog"
    pictures = destination / "buildings"
    pictures.mkdir(exist_ok=True)
    names = {name for _, group in CHAIN_GROUPS for name in group}
    rows = sorted(((SOURCES[n.casefold()].style, n, SOURCES[n.casefold()]) for n in names),
                  key=lambda row: (STYLE_NAMES.get(row[0], row[0]), row[1].casefold()))
    heading = ImageFont.truetype(str(SIGN_FONT), 19)
    caption = ImageFont.truetype(str(SIGN_FONT), 13)
    lines = ["# Storefront building catalog", "",
             f"{len(rows)} map references. Each image shows an exported standalone model on a rectangular "
             "sample site with a centered entrance. Actual footprints, wall materials and entrances vary. "
             "A tenant in a shared building may receive only a sign or a strip of frontage.", "",
             "[Contribution and scaling guide](README.md) · [Downloadable sign chart](storefront-reference.html)", ""]
    lines += ["## Businesses outside the chain catalog", "",
              "A named business with a supported map category can use that family’s architecture on an exclusively matched "
              "standalone footprint. Its sign uses neutral lettering and the verified map name, or the category label "
              "when mapped names are disabled. Exterior tenants use their own frontage. Drive-in carhop geometry requires "
              "separate site evidence.", "",
              "![Category-only storefronts on sample footprints](buildings/category-examples.jpg)", "",
              "| Building family | Category sign | Accepted map categories |", "| --- | --- | --- |"]
    for key, rule in sorted(CATEGORY_RULES.items(), key=lambda item: STYLE_NAMES[item[0]]):
        tags = ", ".join(f"`{tag}`" for tag in rule["categories"])
        lines.append(f"| {STYLE_NAMES[key]} | {rule['label']} | {tags} |")
    lines.append("")
    with tempfile.TemporaryDirectory() as temporary:
        examples = ["cafe", "pizzeria", "hardware", "market", "department_store", "corner_shop"]
        sheet = Image.new("RGB", (3 * 360, 2 * 174), "#eff2ee")
        draw = ImageDraw.Draw(sheet)
        for i, key in enumerate(examples):
            identity = identity_for("Example business", key)
            img, _ = sample("Example business", identity, Path(temporary), category=key)
            x, y = i % 3 * 360, i // 3 * 174
            sheet.paste(img.resize((360, 139), Image.Resampling.LANCZOS), (x, y))
            draw.text((x + 12, y + 138), identity.label, font=heading, fill="#183633")
        sheet.save(pictures / "category-examples.jpg", quality=89, optimize=True)
        for style, family in groupby(rows, key=lambda row: row[0]):
            group = list(family)
            columns = min(3, len(group))
            sheet = Image.new("RGB", (columns * 360, ((len(group) + columns - 1) // columns) * 192), "#eff2ee")
            draw = ImageDraw.Draw(sheet)
            title = STYLE_NAMES.get(style, style)
            finish = STYLES[style].get("wall_finish")
            lines.extend([f"## {title}", ""])
            if finish:
                lines.extend([f"Standalone buildings use {finish.replace('_', ' ')} when no wall material is mapped or observed.", ""])
            lines.extend([f"![{title} standalone samples](buildings/{style}.jpg)", "",
                          "| Map reference | In-game sign | Entrance shown | Available entrances |",
                          "| --- | --- | --- | --- |"])
            for i, (_, source, identity) in enumerate(group):
                image, variant = sample(source, identity, Path(temporary))
                x, y = i % columns * 360, i // columns * 192
                sheet.paste(image.resize((360, 139), Image.Resampling.LANCZOS), (x, y))
                draw.text((x + 12, y + 137), identity.label, font=heading, fill="#183633")
                draw.text((x + 13, y + 163), source, font=caption, fill="#425b55")
                draw.line((x + 12, y + 189, x + 347, y + 189), fill="#c6d3ce")
                forms = ", ".join(form.replace("_", " ") for form in identity.variants)
                lines.append(f"| {source} | {identity.label} | {variant.replace('_', ' ')} | {forms} |")
            sheet.save(pictures / f"{style}.jpg", quality=89, optimize=True)
            lines.append("")
    (destination / "buildings.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    build()
