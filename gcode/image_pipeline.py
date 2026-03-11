import abc
import io
from io import StringIO

import numpy as np
import vtracer
from PIL import Image
from shapely.affinity import rotate
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon
from shapely.ops import substring, unary_union
from skimage import io as skio
from skimage.morphology import skeletonize
from svgelements import SVG, Close, Matrix, Path
from svgelements import Polygon as SVGPolygon

from .config import DEFAULT, ONE_BIT


class SVGConversionPipeline(abc.ABC):
    @abc.abstractmethod
    def convert(self, image_bytes: bytes) -> str:
        pass


class VTracerBinaryPipeline(SVGConversionPipeline):
    def __init__(self, mode="spline", colormode="binary", filter_speckle=4, path_precision=3,
                 fill_enabled=False, fill_spacing=0.1, fill_angle=0):
        self.mode = mode
        self.colormode = colormode
        self.filter_speckle = filter_speckle
        self.path_precision = path_precision
        self.fill_enabled = fill_enabled
        self.fill_spacing = fill_spacing
        self.fill_angle = fill_angle

    def convert(self, image_bytes: bytes) -> str:
        svg_output = vtracer.convert_raw_image_to_svg(
            image_bytes,
            colormode=self.colormode,
            mode=self.mode,
            filter_speckle=self.filter_speckle,
            path_precision=self.path_precision
        )

        if not self.fill_enabled:
            return svg_output

        fill_paths = self._generate_hatch_fill_v2(svg_output)
        return svg_output.replace("</svg>", f"{fill_paths}</svg>")

    def _generate_hatch_fill_v2(self, svg_str):
        svg = SVG.parse(StringIO(svg_str))
        polygons_to_fill = []

        for element in svg.elements():
            if isinstance(element, Path):
                for sub in element.as_subpaths():
                    path_part = Path(sub)

                    has_close_command = any(isinstance(seg, Close) for seg in path_part)
                    p1 = path_part.first_point
                    p2 = path_part.current_point
                    is_visually_closed = (p1 is not None and p2 is not None and
                                          abs(p1.x - p2.x) < 1e-5 and abs(p1.y - p2.y) < 1e-5)

                    if not (has_close_command or is_visually_closed):
                        continue

                    m = element.transform
                    points = [(m.a * p.x + m.c * p.y + m.e, m.b * p.x + m.d * p.y + m.f)
                              for p in path_part.as_points()]

                    if len(points) < 4: continue

                    poly = Polygon(points).buffer(0)
                    if poly.area > 0.05:
                        polygons_to_fill.append(poly)

        if not polygons_to_fill: return ""

        merged = unary_union(polygons_to_fill)
        fill_lines = self._calculate_fill_lines(merged)

        if not fill_lines.is_empty:
            d_str = self._to_svg_path(fill_lines)
            return f'<path d="{d_str}" stroke="red" stroke-width="0.5" fill="none" />'
        return ""

    def _generate_hatch_fill(self, svg_str):
        svg = SVG.parse(StringIO(svg_str))
        all_fill_paths = []

        for element in svg.elements():
            if isinstance(element, Path) and len(element) > 0:
                coords = []
                m = element.transform
                for segment in element:
                    if hasattr(segment, 'end'):
                        px, py = segment.end.x, segment.end.y
                        new_x = m.a * px + m.c * py + m.e
                        new_y = m.b * px + m.d * py + m.f
                        coords.append((new_x, new_y))

                if len(coords) < 3:
                    continue

                poly = Polygon(coords)
                if not poly.is_valid:
                    poly = poly.buffer(0)

                fill_lines = self._calculate_fill_lines(poly)

                if not fill_lines.is_empty:
                    path_data = self._to_svg_path(fill_lines)
                    all_fill_paths.append(f'<path d="{path_data}" stroke="black" stroke-width="0.1" fill="none" />')

        return "\n".join(all_fill_paths)

    def _calculate_fill_lines(self, polygon_obj):
        minx, miny, maxx, maxy = polygon_obj.bounds
        diag = np.sqrt((maxx - minx) ** 2 + (maxy - miny) ** 2)
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2

        final_lines = []
        y = cy - diag / 2
        flip = False

        while y <= cy + diag / 2:
            line = LineString([(cx - diag / 2, y), (cx + diag / 2, y)])
            if self.fill_angle != 0:
                line = rotate(line, self.fill_angle, origin=(cx, cy))

            inter = line.intersection(polygon_obj)

            current_row_lines = []
            if inter.is_empty:
                pass
            elif inter.geom_type == 'LineString':
                current_row_lines.append(inter)
            elif inter.geom_type == 'MultiLineString':
                current_row_lines.extend(list(inter.geoms))
            elif inter.geom_type == 'GeometryCollection':
                for geom in inter.geoms:
                    if geom.geom_type == 'LineString':
                        current_row_lines.append(geom)

            if current_row_lines:
                if flip:
                    current_row_lines.reverse()
                    current_row_lines = [LineString(list(l.coords)[::-1]) for l in current_row_lines]
                final_lines.extend(current_row_lines)
                flip = not flip

            y += self.fill_spacing

        return MultiLineString(final_lines)

    def _to_svg_path(self, multi_line):
        path_parts = []
        for line in multi_line.geoms:
            coords = list(line.coords)
            if len(coords) >= 2:
                path_parts.append(f"M {coords[0][0]:.3f},{coords[0][1]:.3f}")
                for pt in coords[1:]:
                    path_parts.append(f"L {pt[0]:.3f},{pt[1]:.3f}")
        return " ".join(path_parts)


class OneBitPipeline(SVGConversionPipeline):
    def __init__(self, mode="spline", filter_speckle=4, path_precision=3, threshold=128):
        self.mode = mode
        self.filter_speckle = filter_speckle
        self.path_precision = path_precision
        self.threshold = threshold

    def convert(self, image_bytes: bytes) -> str:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = img.point(lambda p: 255 if p > self.threshold else 0, mode='1')

        byte_io = io.BytesIO()
        img.save(byte_io, format="PNG")
        processed_bytes = byte_io.getvalue()

        return vtracer.convert_raw_image_to_svg(
            processed_bytes,
            colormode="binary",
            mode=self.mode,
            filter_speckle=self.filter_speckle,
            path_precision=self.path_precision
        )


class SkeletonPipeline(SVGConversionPipeline):
    def __init__(self, mode="spline", filter_speckle=4, path_precision=3, threshold=0.5):
        self.mode = mode
        self.filter_speckle = filter_speckle
        self.path_precision = path_precision
        self.threshold = threshold

    def convert(self, image_bytes: bytes) -> str:
        image = skio.imread(io.BytesIO(image_bytes), as_gray=True)
        binary = image < self.threshold
        skeleton = skeletonize(binary)

        skeleton_img = Image.fromarray((~skeleton * 255).astype(np.uint8))

        byte_io = io.BytesIO()
        skeleton_img.save(byte_io, format="PNG")
        processed_bytes = byte_io.getvalue()

        return vtracer.convert_raw_image_to_svg(
            processed_bytes,
            colormode="binary",
            mode=self.mode,
            filter_speckle=self.filter_speckle,
            path_precision=self.path_precision
        )


class SVGConverter:
    def __init__(self, fill_on=False, spacing=0, angle=0):
        self.pipelines = {
            ONE_BIT: OneBitPipeline(),
            DEFAULT: VTracerBinaryPipeline(mode="polygon", fill_enabled=fill_on, fill_spacing=spacing, fill_angle=angle,
                                           filter_speckle=10),
        }

    def convert(self, image_bytes: bytes, pipeline_name: str) -> str:
        pipeline = self.pipelines.get(pipeline_name)
        if not pipeline:
            raise ValueError(f"Unknown pipeline: {pipeline_name}")
        return pipeline.convert(image_bytes)
