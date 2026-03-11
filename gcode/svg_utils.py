from io import StringIO

from svgelements import SVG


def get_svg_bbox(svg_raw_str):
    if "<svg" not in svg_raw_str:
        full_svg_str = f'<svg xmlns="http://www.w3.org/2000/svg">{svg_raw_str}</svg>'
    else:
        full_svg_str = svg_raw_str

    svg_obj = SVG.parse(StringIO(full_svg_str))
    bbox = svg_obj.bbox()
    return bbox


def filter_curves(curves, min_distance=0.2):
    new_curves = []
    for curve in curves:
        dist = ((curve.end.x - curve.start.x) ** 2 + (curve.end.y - curve.start.y) ** 2) ** 0.5
        if dist >= min_distance:
            new_curves.append(curve)
    return new_curves
