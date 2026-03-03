import streamlit as st
import vtracer
from svg_to_gcode.svg_parser import parse_string
from svg_to_gcode.compiler import Compiler, interfaces
from enum import Enum
import re
import abc
import io
from PIL import Image
import numpy as np
from skimage.morphology import skeletonize
from skimage import io as skio
from svgelements import SVG, Path, Close, Matrix, Polygon as SVGPolygon
from shapely.geometry import Polygon, LineString, MultiLineString, MultiPolygon
from shapely.affinity import rotate
from shapely.ops import unary_union
from io import StringIO  # 必須引入這個
from shapely.geometry import LineString, MultiLineString, GeometryCollection
from shapely.ops import substring
import math
from collections import defaultdict

# from svg_to_gcode import TOLERANCES
# TOLERANCES["operation"] = 0.2
MM_TO_PX = 96.0 / 25.4
PX_TO_MM = 25.4 / 96.0

ONE_BIT = "黑白預處理 (One-bit)"
DEFAULT = "快速掃描 (Quick)"

st.title("🎨 影像轉 G-code 產生器")
st.subheader("支援 JPG, PNG 向量化並統一輸出 G-code")


class WorkMode(Enum):
    LASER = "Laser"
    CNC = "CNC"
    PEN = "Pen"

# --- SVG 轉換 Pipeline ---
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

        # 新增填滿參數
        self.fill_enabled = fill_enabled
        self.fill_spacing = fill_spacing  # 線距 (mm)
        self.fill_angle = fill_angle  # 填充角度

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

            # 2. 如果啟用填滿，則需要額外處理
            # 這裡的邏輯是：解析 SVG -> 產生填充線 -> 將填充線轉為 SVG Path 併入
        fill_paths = self._generate_hatch_fill_v2(svg_output)

        # 3. 將填滿路徑插入到原始 SVG 的 </svg> 標籤之前
        return svg_output.replace("</svg>", f"{fill_paths}</svg>")

    def _generate_hatch_fill_v2(self, svg_str):
        svg = SVG.parse(StringIO(svg_str))
        polygons_to_fill = []

        for element in svg.elements():
            if isinstance(element, Path):
                # 1. 這裡改用 element.as_subpaths() 並轉換為獨立 Path
                for sub in element.as_subpaths():
                    path_part = Path(sub)

                    # --- 修正後的閉合判斷 ---
                    # 方法 A: 檢查指令中是否有 Close (Z)
                    # 方法 B: 檢查首尾點距離是否小於極小值 (1e-5)
                    has_close_command = any(isinstance(seg, Close) for seg in path_part)
                    p1 = path_part.first_point
                    p2 = path_part.current_point
                    is_visually_closed = (p1 is not None and p2 is not None and
                                          abs(p1.x - p2.x) < 1e-5 and abs(p1.y - p2.y) < 1e-5)

                    if not (has_close_command or is_visually_closed):
                        continue

                    # 2. 使用更穩定的 as_points 並限制採樣密度
                    m = element.transform
                    # 這裡的 .approximate_arcs() 能防止曲線變成太碎的點
                    points = [(m.a * p.x + m.c * p.y + m.e, m.b * p.x + m.d * p.y + m.f)
                              for p in path_part.as_points()]

                    if len(points) < 4: continue  # 至少要四個點才能成面

                    poly = Polygon(points).buffer(0)
                    # 3. 過濾掉面積太小的雜訊（避免微小色塊）
                    if poly.area > 0.05:
                        polygons_to_fill.append(poly)

        if not polygons_to_fill: return ""

        # 4. 合併幾何體 (處理環形與重疊)
        merged = unary_union(polygons_to_fill)

        # 5. 生成線段 (確保這部分的 _calculate_fill_lines 回傳的是 LineString)
        fill_lines = self._calculate_fill_lines(merged)

        if not fill_lines.is_empty:
            d_str = self._to_svg_path(fill_lines)
            # 強制指定 fill="none" 並加粗線條顏色以便觀察
            return f'<path d="{d_str}" stroke="red" stroke-width="0.5" fill="none" />'
        return ""

    def _generate_hatch_fill(self, svg_str):
        # 1. 使用 svgelements 解析 SVG 字串
        svg = SVG.parse(StringIO(svg_str))
        all_fill_paths = []

        # 2. 遍歷所有路徑
        for element in svg.elements():
            if isinstance(element, Path) and len(element) > 0:
                coords = []
                # 獲取該元素的變換矩陣 (重要：這解決了之前的 translate 問題)
                m = element.transform
                for segment in element:
                    if hasattr(segment, 'end'):
                        # 這是最穩定的手動點變換邏輯：
                        # x' = a*x + c*y + e
                        # y' = b*x + d*y + f
                        px, py = segment.end.x, segment.end.y
                        new_x = m.a * px + m.c * py + m.e
                        new_y = m.b * px + m.d * py + m.f
                        coords.append((new_x, new_y))

                if len(coords) < 3:
                    continue

                # 3. 建立 Shapely 多邊形
                poly = Polygon(coords)
                if not poly.is_valid:
                    poly = poly.buffer(0)  # 修復自相交的無效多邊形

                # 4. 生成填充線段
                fill_lines = self._calculate_fill_lines(poly)

                # 5. 將 Shapely 線段轉回 SVG Path Data (d string)
                if not fill_lines.is_empty:
                    path_data = self._to_svg_path(fill_lines)
                    all_fill_paths.append(f'<path d="{path_data}" stroke="black" stroke-width="0.1" fill="none" />')

        return "\n".join(all_fill_paths)

    def _calculate_fill_lines(self, polygon_obj):
        """計算多邊形內部的填充線，並交替方向以減少空跑時間"""

        minx, miny, maxx, maxy = polygon_obj.bounds
        diag = np.sqrt((maxx - minx) ** 2 + (maxy - miny) ** 2)
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2

        final_lines = []
        y = cy - diag / 2
        flip = False  # 追蹤當前行是否需要反轉方向

        while y <= cy + diag / 2:
            # 產生一條水平長線
            line = LineString([(cx - diag / 2, y), (cx + diag / 2, y)])
            if self.fill_angle != 0:
                line = rotate(line, self.fill_angle, origin=(cx, cy))

            # 取得交集
            inter = line.intersection(polygon_obj)

            # 核心修正：展開所有可能的線段結果，並根據 flip 決定方向
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
                # 如果 flip 為 True，則反轉整行中所有線段的順序，且每條線段本身也要反向
                if flip:
                    # 先反轉線段列表的順序（例如原本有兩段，從左往右，現在要從右往左，先處理最右邊那段）
                    current_row_lines.reverse()
                    # 再反轉每條線段內部的座標點順序
                    current_row_lines = [LineString(list(l.coords)[::-1]) for l in current_row_lines]
                
                final_lines.extend(current_row_lines)
                flip = not flip  # 切換下一行的方向

            y += self.fill_spacing

        # 直接回傳封裝好的 MultiLineString 物件
        return MultiLineString(final_lines)

    def _to_svg_path(self, multi_line):
        """修正：遍歷 MultiLineString 的幾何體"""
        path_parts = []
        # 使用 .geoms 來安全地迭代子線段
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
        # 1. 影像預處理：轉換為單色 (One-bit)
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        # 套用閥值
        img = img.point(lambda p: 255 if p > self.threshold else 0, mode='1')
        
        # 轉回 bytes
        byte_io = io.BytesIO()
        img.save(byte_io, format="PNG")
        processed_bytes = byte_io.getvalue()

        # 2. 呼叫 vtracer 進行向量化
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
        # 1. 讀取影像並轉為灰階
        image = skio.imread(io.BytesIO(image_bytes), as_gray=True)
        # 2. 轉為二值圖 (0 或 1)
        binary = image < self.threshold
        # 3. 骨架化：把粗線變成 1 像素寬的中心線
        skeleton = skeletonize(binary)
        
        # 4. 將布林矩陣轉回影像並存為 bytes
        # skeleton 是布林矩陣，True 為 1 (線條), False 為 0 (背景)
        # 為了讓 vtracer 正確識別，我們將線條轉為黑色 (0)，背景轉為白色 (255)
        skeleton_img = Image.fromarray((~skeleton * 255).astype(np.uint8))
        
        byte_io = io.BytesIO()
        skeleton_img.save(byte_io, format="PNG")
        processed_bytes = byte_io.getvalue()

        # 5. 呼叫 vtracer 進行向量化
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
            # "骨架化中心線 (Skeleton)": SkeletonPipeline(mode="polygon"),
            DEFAULT: VTracerBinaryPipeline(mode="polygon", fill_enabled=fill_on, fill_spacing=spacing, fill_angle=angle,
            filter_speckle=10),
        }

    def convert(self, image_bytes: bytes, pipeline_name: str) -> str:
        pipeline = self.pipelines.get(pipeline_name)
        if not pipeline:
            raise ValueError(f"Unknown pipeline: {pipeline_name}")
        return pipeline.convert(image_bytes)

# --- 1. 機器資料庫配置 ---
MACHINE_DATABASE = {
    "請選擇機器...": {"width": 0, "height": 0, "depth": 0, "modes": [], "default_power": 0},
    # "Cubiio X": {
    #     "width": 1800, "height": 600, "depth": 35, "modes": [WorkMode.LASER, WorkMode.CNC], "default_power": 255,
    # },
    "GORDIX": {
        "width": 2280, "height": 1060, "depth": 35, "modes": [WorkMode.LASER, WorkMode.CNC, WorkMode.PEN], "default_power": 255
    }
}

class ToolHeadShape(Enum):
    TAPER_TIP = "taper_tip"   # Standard laser or needle
    FLAT_END = "flat_end"    # Square end mill
    BALL_END = "ball_end"  # Rounded end mill

TOOL_DATABASE = {
    "尖頭#1": {"diameter": 3.175, "shape": ToolHeadShape.TAPER_TIP, "angle": 5.3, "flutes": 2},
    "平頭#1": {"diameter": 6.0, "shape": ToolHeadShape.FLAT_END, "angle": 0.0, "flutes": 1},
    "圓頭#1": {"diameter": 3.175, "shape": ToolHeadShape.BALL_END, "angle": 0.0, "flutes": 1},
}

MATERIAL_DATABASE = {
    "不鏽鋼 (Stainless Steel)": {"name": "不鏽鋼", "vc": [15, 30], "fz": 0.01},
    "鐵 (Iron/Steel)": {"name": "鐵", "vc": [20, 50], "fz": 0.02},
    "鋁 (Aluminum)": {"name": "鋁", "vc": [150, 300], "fz": 0.04},
    "銅 (Copper)": {"name": "銅", "vc": [80, 150], "fz": 0.03},
    "硬質塑膠 (Acrylic/POM)": {"name": "硬質塑膠", "vc": [100, 250], "fz": 0.05},
    "軟質塑膠 (PVC/PE)": {"name": "軟質塑膠", "vc": [150, 300], "fz": 0.08},
    "硬木 (Hardwood)": {"name": "硬木", "vc": [300, 500], "fz": 0.1},
    "軟木/合成板 (Softwood/MDF)": {"name": "軟木", "vc": [400, 600], "fz": 0.15},
}

def get_dial(rpm):
    map_data = [
        {"dial": 1, "rpm": 10000},
        {"dial": 2, "rpm": 12000},
        {"dial": 3, "rpm": 17000},
        {"dial": 4, "rpm": 22000},
        {"dial": 5, "rpm": 27000},
        {"dial": 6, "rpm": 30000}
    ]
    if rpm <= 10000: return "1"
    if rpm >= 30000: return "6"
    for i in range(len(map_data) - 1):
        if rpm >= map_data[i]["rpm"] and rpm <= map_data[i+1]["rpm"]:
            ratio = (rpm - map_data[i]["rpm"]) / (map_data[i+1]["rpm"] - map_data[i]["rpm"])
            return f"{map_data[i]['dial'] + ratio:.1f}"
    return "N/A"

def calculate_cnc_params(tool_name, material_name):
    tool = TOOL_DATABASE[tool_name]
    material = MATERIAL_DATABASE[material_name]
    
    # Calculate target RPM based on average Vc
    # Vc = (RPM * PI * Diameter) / 1000
    # RPM = (Vc * 1000) / (PI * Diameter)
    target_vc = (material["vc"][0] + material["vc"][1]) / 2
    rpm = (target_vc * 1000) / (np.pi * tool["diameter"])
    
    # Clamp RPM to machine limits (Makita RT0700 10,000-30,000)
    rpm = max(10000, min(30000, rpm))
    
    # Feedrate (mm/min) calculation: RPM * Flutes * Fz (chipload)
    feedrate = rpm * tool.get("flutes", 1) * material["fz"]
    
    # Clamp feedrate to machine limit (F2000)
    feedrate = min(2000, feedrate)
    
    return int(rpm), int(feedrate)

class MySmartCompiler(Compiler):
    def __init__(self, interface_class, movement_speed, cutting_speed, pass_depth, dwell_time=0, unit=None, custom_header=None, custom_footer=None):
        super().__init__(interface_class, movement_speed, cutting_speed, pass_depth, dwell_time, unit, custom_header, custom_footer)

class GCodeOptimizer:
    def __init__(self, stitch_tolerance=0.05):
        self.stitch_tolerance = stitch_tolerance
        self.machine_config = {"on_cmd": "M3", "off_cmd": "M5"}
        self.header = []

    def parse_to_fragments(self, gcode_text):
        """對應 JS parseToFragments: 將 G-code 拆解為獨立線段"""
        lines = gcode_text.splitlines()
        fragments = []
        active_frag = {"points": [], "commands": []}
        cur_x, cur_y, cur_z = 0.0, 0.0, 0.0
        found_motion = False

        for line in lines:
            cmd = line.strip().upper()
            if not cmd or cmd.startswith(('(', ';')): continue

            # 提取 Header
            if not found_motion and re.search(r'G(20|21|90|91|17)', cmd):
                self.header.append(line.strip())
                continue

            # 座標提取
            match_x = re.search(r'X([-+]?\d*\.\d+|\d+)', cmd)
            match_y = re.search(r'Y([-+]?\d*\.\d+|\d+)', cmd)
            match_z = re.search(r'Z([-+]?\d*\.\d+|\d+)', cmd)

            nx = float(match_x.group(1)) if match_x else cur_x
            ny = float(match_y.group(1)) if match_y else cur_y
            nz = float(match_z.group(1)) if match_z else cur_z

            is_g0 = cmd.startswith('G0')
            is_lift = match_z and nz > cur_z
            is_off = "M05" in cmd or "M5" in cmd

            if is_g0 or is_lift or is_off or re.search(r'G[1-3]', cmd):
                found_motion = True

            # 遇到空移或舉刀，結束當前片段
            if (is_g0 or is_lift or is_off) and active_frag["points"]:
                fragments.append(active_frag)
                active_frag = {"points": [], "commands": []}

            # 記錄加工路徑
            if re.search(r'G[1-3]', cmd):
                if not active_frag["points"]:
                    active_frag["points"].append({"x": cur_x, "y": cur_y, "z": cur_z})
                active_frag["points"].append({"x": nx, "y": ny, "z": nz})
                active_frag["commands"].append(line.strip())

            cur_x, cur_y, cur_z = nx, ny, nz

        if active_frag["points"]: fragments.append(active_frag)
        return fragments

    def global_stitch(self, fragments):
        """對應 JS globalStitch: 將斷開的線段縫合成完整的零件 (Parts)"""
        pool = fragments[:]
        parts = []

        while pool:
            active = pool.pop(0)
            expanded = True
            while expanded:
                expanded = False
                tail = active["points"][-1]
                head = active["points"][0]

                for i in range(len(pool)):
                    target = pool[i]
                    t_head = target["points"][0]
                    t_tail = target["points"][-1]

                    # 四種縫合情況：尾-頭, 尾-尾, 頭-尾, 頭-頭
                    if math.hypot(tail['x'] - t_head['x'], tail['y'] - t_head['y']) < self.stitch_tolerance:
                        active["points"] += target["points"][1:]
                        active["commands"] += target["commands"]
                        pool.pop(i);
                        expanded = True;
                        break
                    elif math.hypot(tail['x'] - t_tail['x'], tail['y'] - t_tail['y']) < self.stitch_tolerance:
                        active["points"] += target["points"][::-1][1:]
                        active["commands"] += target["commands"][::-1]
                        pool.pop(i);
                        expanded = True;
                        break

            # 計算邊界 (Bounds)
            pts = active["points"]
            xs, ys = [p['x'] for p in pts], [p['y'] for p in pts]
            active["bounds"] = {"minX": min(xs), "maxX": max(xs), "minY": min(ys), "maxY": max(ys)}
            active["isClosed"] = math.hypot(pts[0]['x'] - pts[-1]['x'],
                                            pts[0]['y'] - pts[-1]['y']) < self.stitch_tolerance
            parts.append(active)
        return parts

    def sort_by_tl_chain(self, parts):
        """對應 JS sortParts (tl-chain): 零件群組化並執行 TL-Chain 排序"""
        # 1. 建立 Groups (大包小, 嵌套邏輯)
        sorted_by_area = sorted(parts, key=lambda p: (p['bounds']['maxX'] - p['bounds']['minX']) * (
                    p['bounds']['maxY'] - p['bounds']['minY']), reverse=True)
        groups = []
        handled = [False] * len(sorted_by_area)

        for i in range(len(sorted_by_area)):
            if handled[i]: continue
            group = {"main": sorted_by_area[i], "children": []}
            handled[i] = True
            a = sorted_by_area[i]['bounds']
            for j in range(len(sorted_by_area)):
                if handled[j]: continue
                b = sorted_by_area[j]['bounds']
                if sorted_by_area[i]['isClosed'] and b['minX'] >= a['minX'] and b['maxX'] <= a['maxX'] and b['minY'] >= \
                        a['minY'] and b['maxY'] <= a['maxY']:
                    group["children"].append(sorted_by_area[j])
                    handled[j] = True
            groups.append(group)

        # 2. TL-Chain 鏈式排序
        if not groups: return []
        sorted_groups = []
        unvisited = groups[:]

        # 初始點選擇 (Score = Y*10 - X)
        def get_score(g):
            first = g['children'][0] if g['children'] else g['main']
            p = first['points'][0]
            return (p['y'] * 10) - p['x']

        start_idx = max(range(len(unvisited)), key=lambda i: get_score(unvisited[i]))
        current = unvisited.pop(start_idx)
        sorted_groups.append(current)
        cur_pos = current['main']['points'][-1]

        # 鏈式搜尋
        while unvisited:
            def dist_to_next(g):
                first = g['children'][0] if g['children'] else g['main']
                p = first['points'][0]
                return math.hypot(p['x'] - cur_pos['x'], p['y'] - cur_pos['y'])

            best_idx = min(range(len(unvisited)), key=lambda i: dist_to_next(unvisited[i]))
            current = unvisited.pop(best_idx)
            sorted_groups.append(current)
            cur_pos = current['main']['points'][-1]

        return sorted_groups


def export_to_gcode(optimized_groups, header, rapid_f=1500, cut_f=600):
    """
    將優化後的 Group 數據結構轉換為可下載的 G-code 字串
    """
    output = ["; Optimized G-code v4.3 (Python Version)"]

    # 1. 加入 Header (G21, G90 等)
    if header:
        output.append("\n".join(header))
    else:
        output.append("G21 G90")

    p_num = 1
    # 這裡假設你的 machine_config
    on_cmd = "M3"  # 或從你的 optimizer 物件取得
    off_cmd = "M5"  # 或從你的 optimizer 物件取得

    # 2. 遍歷 Group 與 Parts
    for group in optimized_groups:
        # 先切 children (內孔)，最後切 main (外框)
        all_parts = group['children'] + [group['main']]

        for p in all_parts:
            output.append(f"\n; --- Part #{p_num} ({'Closed' if p['isClosed'] else 'Open'}) ---")
            p_num += 1

            # 移動到起點 (G0)
            start = p['points'][0]
            output.append(f"G0 X{start['x']:.3f} Y{start['y']:.3f} F{rapid_f}")

            # 下刀/開雷射
            output.append(on_cmd)

            # 寫入加工路徑指令
            for idx, cmd in enumerate(p['commands']):
                # 移除原始 F 值並統一插入我們設定的 cut_f
                clean_cmd = re.sub(r'F[0-9.]+', '', cmd).strip()
                if idx == 0:
                    output.append(f"{clean_cmd} F{cut_f}")
                else:
                    output.append(clean_cmd)

            # 舉刀/關雷射
            output.append(off_cmd)

    output.append("\n; --- Job Finished ---\nG0 X0 Y0\nM30")
    return "\n".join(output)

# --- 2. 自定義 Interface ---
class AdaptiveInterface(interfaces.Gcode):
    def __init__(self, mode=WorkMode.LASER, power=255, speed=1500, rpm=0):
        super().__init__()
        self.mode = mode
        self.power = power
        self.rpm = rpm
        self.set_laser_power(1)
        super().set_movement_speed(speed)
        self.precision = 4

    def header(self):
        return "G90 G21"

    def footer(self):
        return [self.laser_off(), f"G0 X0 Y0;"]

    def arc_move(self, x=None, y=None, i=None, j=None, clockwise=True):
        # G2 是順時針，G3 是逆時針
        command = "G2" if clockwise else "G3"

        # 格式化座標與中心偏移量 (I, J)
        coords = []
        if x is not None: coords.append(f"X{x:.3f}")
        if y is not None: coords.append(f"Y{y:.3f}")
        if i is not None: coords.append(f"I{i:.3f}")  # 圓心相對起點的 X 偏移
        if j is not None: coords.append(f"J{j:.3f}")  # 圓心相對起點的 Y 偏移

        return f"{command} {' '.join(coords)};"


def get_svg_bbox(svg_raw_str):
    # 1. 確保字串被完整的 <svg> 標籤包裹，否則解析器可能無法正確讀取路徑
    if "<svg" not in svg_raw_str:
        full_svg_str = f'<svg xmlns="http://www.w3.org/2000/svg">{svg_raw_str}</svg>'
    else:
        full_svg_str = svg_raw_str

    # 2. 使用 StringIO 將字串模擬為檔案供解析器讀取
    svg_obj = SVG.parse(StringIO(full_svg_str))

    # 3. 獲取內容的邊界框
    # 回傳值 bbox 為 (xmin, ymin, xmax, ymax) 的元組
    bbox = svg_obj.bbox()

    return bbox


def filter_curves(curves, min_distance=0.2):
    """
    過濾掉距離過近的點，強制讓兩點間距大於指定值
    """
    new_curves = []
    for curve in curves:
        # 計算線段長度
        # 使用歐幾里得距離公式: sqrt((x2-x1)^2 + (y2-y1)^2)
        dist = ((curve.end.x - curve.start.x) ** 2 + (curve.end.y - curve.start.y) ** 2) ** 0.5

        if dist >= min_distance:
            new_curves.append(curve)
        # 如果太短，可以選擇忽略，或將其合併到下一段（這裡採忽略較簡單）
    return new_curves


# --- 3. 側邊欄 UI ---
with st.sidebar:
    st.header("🤖 機器配置")
    
    # 使用 key 確保唯一性
    machine_name = st.selectbox("1. 選擇機器型號", list(MACHINE_DATABASE.keys()), key="machine_select")
    machine_info = MACHINE_DATABASE[machine_name]

    work_mode = None
    feedrate = 1500 # 預設值

    if machine_name != "請選擇機器...":
        st.info(f"📏 範圍: {machine_info['width']}x{machine_info['height']}x{machine_info['depth']} mm")

        work_mode = st.radio(
            "2. 選擇工作模式",
            machine_info["modes"],
            format_func=lambda x: x.value,
            key="mode_radio"
        )

        rotation_speed = 0 # Only for CNC
        if work_mode == WorkMode.CNC:
            st.subheader("🛠️ CNC 設定")
            tool_info = st.selectbox("選擇鑽頭", list(TOOL_DATABASE.keys()), key="tool_select")
            selected_material = st.selectbox("選擇材料", list(MATERIAL_DATABASE.keys()), key="material_select")
            
            # 重新計算速度
            rotation_speed, feedrate = calculate_cnc_params(tool_info, selected_material)
            
            st.success(f"📈 建議參數:\n- 主軸轉速: {rotation_speed} RPM (撥盤: {get_dial(rotation_speed)})\n- 進給速度: {feedrate} mm/min")
            
            # 讓使用者可以微調
            feedrate = st.number_input("進給速度 (mm/min)", value=feedrate, step=50)
            rotation_speed = st.number_input("主軸轉速 (RPM)", value=rotation_speed, step=100)

        elif work_mode == WorkMode.LASER or work_mode == WorkMode.PEN:
            feedrate = st.slider("雕刻速度 (mm/min)", 100, 5000, 1500)
        else:
            feedrate = 1500

        # 在 Streamlit 中呼叫
        fill_on = False
        spacing = 0.05
        if work_mode == WorkMode.LASER or work_mode == WorkMode.PEN:
            st.subheader("🖼️ 轉檔設定")
            fill_on = st.sidebar.checkbox("啟用填滿", value=True)
            if fill_on:
                spacing = st.sidebar.slider("填滿間隔 (mm)", 0.2, 5.0, 0.05)

        svg_converter = SVGConverter(fill_on, spacing, 0)
        # selected_pipeline = st.selectbox(
        #     "選擇轉檔 Pipeline",
        #     list(svg_converter.pipelines.keys()),
        #     key="pipeline_select"
        # )


    else:
        st.warning("請先選擇機器")

# --- 4. 主畫面轉換邏輯 ---
uploaded_file = st.file_uploader("上傳圖片或 SVG", type=["jpg", "png", "svg"])
if uploaded_file and machine_name != "請選擇機器...":
    file_ext = uploaded_file.name.split('.')[-1].lower()

    # 1. 取得 SVG 內容
    if file_ext in ["jpg", "jpeg", "png"]:
        input_bytes = uploaded_file.getvalue()
        # 使用 SVGConverter 進行轉檔
        svg_raw = svg_converter.convert(input_bytes, DEFAULT)
    else:
        svg_raw = uploaded_file.getvalue().decode("utf-8")

    # 修正：更精確地提取 SVG 內部標籤，保留 circle, ellipse, path, rect 等
    # 使用正則表達式移除 <svg> 開頭與結尾，但保留內容
    inner_svg_content = re.sub(r'<\?xml.*?\?>', '', svg_raw)  # 移除 XML 宣告
    inner_svg_content = re.sub(r'<svg[^>]*>', '', inner_svg_content)  # 移除開頭標籤
    inner_svg_content = inner_svg_content.replace('</svg>', '')  # 移除結尾標籤

    st.divider()
    st.subheader("🖼️ 圖稿位置與尺寸調整")

    # --- 1. 機器物理與像素尺寸轉換 ---
    # 機器物理尺寸 (mm) - 2280x1060
    m_w_mm, m_h_mm = machine_info["width"], machine_info["height"]
    m_w_half_mm, m_h_half_mm = m_w_mm / 2, m_h_mm / 2

    # 顯示用的像素尺寸 (px)
    m_w_px = m_w_mm * MM_TO_PX
    m_h_px = m_h_mm * MM_TO_PX
    m_w_half_px, m_h_half_px = m_w_px / 2, m_h_px / 2

    # --- 2. 解析原始 SVG 內容尺寸 (px) ---
    svg_width_px = 0
    svg_height_px = 0
    viewbox_match = re.search(r'viewBox="([\d\.\s,-]+)"', svg_raw)
    width_match = re.search(r'width="([\d\.]+)"', svg_raw)
    height_match = re.search(r'height="([\d\.]+)"', svg_raw)
    print(viewbox_match, width_match, height_match)

    if viewbox_match:
        vb_parts = re.split(r'[\s,]+', viewbox_match.group(1).strip())
        if len(vb_parts) == 4:
            svg_width_px = float(vb_parts[2])
            svg_height_px = float(vb_parts[3])
    elif width_match and height_match:
        svg_width_px = float(width_match.group(1))
        svg_height_px = float(height_match.group(1))

    print(svg_width_px, svg_height_px)

    # 計算初始縮放 (讓圖片佔滿機器像素區域的 80%)
    auto_scale_calc = min(m_w_px * 0.8 / svg_width_px, m_h_px * 0.8 / svg_height_px) if svg_width_px > 0 else 1.0

    # --- 3. UI 滑桿：使用 mm 單位 ---
    col_adj1, col_adj2, col_adj3 = st.columns(3)
    with col_adj1:
        manual_scale = st.slider("物件縮放", 0.01, 5.0, float(auto_scale_calc), 0.01)
    with col_adj2:
        off_x_mm = st.slider("水平位移 (X mm)", -m_w_half_mm, m_w_half_mm, 0.0, 0.5)
    with col_adj3:
        off_y_mm = st.slider("垂直位移 (Y mm)", -m_h_half_mm, m_h_half_mm, 0.0, 0.5)

    # --- 4. 座標校正與變換字串構建 (預覽用) ---
    # 【核心修正】：預覽時，所有 translate 必須是像素單位 px

    # A. 使用者指定的位移 (mm -> px)
    off_x_px = off_x_mm * MM_TO_PX
    off_y_px = off_y_mm * MM_TO_PX

    # B. 將圖片內容中心移至 (0,0) 的像素偏移量
    # 這裡直接用負的半寬與半高，不需要乘以 PX_TO_MM
    auto_off_x_px = -(svg_width_px / 2)
    auto_off_y_px = - (svg_height_px / 2)
    bbox = get_svg_bbox(svg_raw)
    if bbox:
        xmin, ymin, xmax, ymax = bbox

        # 計算內容的真實幾何中心點 (px)
        real_center_x = (xmin + xmax) / 2
        show_center_y = (ymin + ymax) / 2
        real_center_y = show_center_y - svg_height_px

        # 這是您需要的萬用自動偏移量
        # 無論圖案在哪裡，這組位移都會將「內容中心」拉回 (0,0)
        auto_off_x_px = -real_center_x
        show_center_y = -show_center_y
        auto_off_y_px = -real_center_y

        print(f"內容邊界: X({xmin} to {xmax}), Y({ymin} to {ymax})")
        print(f"建議偏移: X={auto_off_x_px}, Y={auto_off_y_px}")
    print(auto_off_x_px, auto_off_y_px)

    # C. 構建預覽用的 transform
    # 邏輯：先將圖片中心移到原點 -> 縮放 -> 移到使用者指定的機器位置
    transform_str = f"translate({off_x_px}, {off_y_px}) scale({manual_scale}) translate({auto_off_x_px}, {show_center_y})"

    # --- 5. 生成預覽 SVG ---
    # 使用像素尺寸定義畫布，但 viewBox 將 (0,0) 設置在中央
    dynamic_font_size = int(m_w_px * 0.02)
    label_offset = int(m_w_px * 0.01)

    current_obj_w_mm = svg_width_px * manual_scale * PX_TO_MM
    current_obj_h_mm = svg_height_px * manual_scale * PX_TO_MM
    # 2. 定義獨立的標籤字串
    # 注意：這些座標是相對於 viewBox 的中心點 (0,0)
    # 獲取縮放與位移後的圖片中心與邊界 (像素)
    # 這裡的座標是相對於 viewBox 的 (0,0) 中心點
    obj_center_x_px = off_x_mm * MM_TO_PX
    obj_center_y_px = off_y_mm * MM_TO_PX

    # 計算圖片縮放後的像素寬高
    obj_w_px = (bbox[2] - bbox[0]) * manual_scale
    obj_h_px = (bbox[3] - bbox[1]) * manual_scale

    # 定義文字與圖片的間距 (Gap)
    text_gap = dynamic_font_size * 0.5
    label_tags = f"""
    <text x="{obj_center_x_px}" y="{obj_center_y_px - (obj_h_px / 2) - text_gap}" 
          text-anchor="middle" font-family="sans-serif" font-size="{dynamic_font_size * 0.8}" 
          fill="red" font-weight="bold">
        Width: {current_obj_w_mm:.1f} mm
    </text>

    <text x="{obj_center_x_px + (obj_w_px / 2) + text_gap}" y="{obj_center_y_px}" 
          dominant-baseline="middle" font-family="sans-serif" font-size="{dynamic_font_size * 0.8}" 
          fill="red" font-weight="bold">
        Height: {current_obj_h_mm:.1f} mm
    </text>
    """
    transformed_svg = f"""<svg width="{m_w_px}" height="{m_h_px}" viewBox="{-m_w_half_px} {-m_h_half_px} {m_w_px} {m_h_px}" xmlns="http://www.w3.org/2000/svg">
        <rect x="{-m_w_half_px}" y="{-m_h_half_px}" width="{m_w_px}" height="{m_h_px}" fill="white" />
        <line x1="{-m_w_half_px}" y1="0" x2="{m_w_half_px}" y2="0" stroke="red" stroke-width="1" stroke-dasharray="5,5" opacity="0.5" />
        <line x1="0" y1="{-m_h_half_px}" x2="0" y2="{m_h_half_px}" stroke="red" stroke-width="1" stroke-dasharray="5,5" opacity="0.5" />
        <text x="{-m_w_half_px + label_offset}" y="{-m_h_half_px + dynamic_font_size}" 
          font-family="sans-serif" font-size="{dynamic_font_size}" font-weight="bold" fill="#555555">
            Machine: {m_w_mm} x {m_h_mm} mm
        </text>
        {label_tags}
        <g transform="{transform_str}">
            {inner_svg_content}
        </g>
    </svg>"""

    transform_str_c = f"translate({off_x_px}, {off_y_px}) scale({manual_scale}) translate({auto_off_x_px}, {auto_off_y_px})"
    convert_svg = f"""<svg width="{svg_width_px * manual_scale}" height="{svg_height_px * manual_scale}" xmlns="http://www.w3.org/2000/svg">
        <g transform="{transform_str_c}">
            {inner_svg_content}
        </g>
    </svg>"""

    # 在 Streamlit 預覽 (加上 CSS 讓它好看一點)
    st.markdown("##### 預覽圖 (紅線為機器原點)")
    st.image(transformed_svg, use_container_width=True)
    #
    # --- 在預覽圖下方加入下載 SVG 按鈕 ---
    st.download_button(
        label="📥 下載調整後的 SVG 檔案",
        data=convert_svg,
        file_name=f"{uploaded_file.name.split('.')[0]}_adjusted.svg",
        mime="image/svg+xml"
    )

    # --- 第三階段：修正後的編譯與字串處理 ---
    if st.button("🚀 開始轉換為特定格式 G-code"):
        try:
            my_interface = AdaptiveInterface(mode=work_mode, power=machine_info['default_power'], speed=feedrate, rpm=rotation_speed)
            compiler = MySmartCompiler(
                lambda: my_interface,
                movement_speed=feedrate,
                cutting_speed=feedrate,
                pass_depth=1.0 if work_mode == WorkMode.CNC else 0,
                unit="mm",
                custom_header=[],
                custom_footer=my_interface.footer()
            )

            curves = filter_curves(parse_string(convert_svg))
            compiler.append_curves(curves)
            
            # 4. 執行編譯
            raw_gcode_body = compiler.compile()
            
            # 過濾掉空白行，並去掉每行前後的空格
            clean_lines = [line.strip() for line in raw_gcode_body.splitlines() if line.strip()]
            final_gcode = "\n".join(clean_lines)

            if fill_on:
                # 定義優化邏輯：
                # 尋找 M5 之後、M3 之前的 G1 指令並改為 G0
                # 模式解釋：
                # (M5;?\s+)          -> 匹配 M5 指令
                # G1(\s+[^M]+?)      -> 匹配 G1 及其後的座標參數，直到遇到下一個 M 指令前
                # (?=\s+M3)          -> 確保後方緊跟著 M3（開啟雷射）

                pattern = r"(M5;?\s+)G1(\s+[^M]+?)(?=\s+M3)"

                # 執行替換
                final_gcode = re.sub(pattern, r"\1G0\2", final_gcode, flags=re.MULTILINE)

            # --- 執行範例 ---
            optimizer = GCodeOptimizer(stitch_tolerance=0.05)
            frags = optimizer.parse_to_fragments(final_gcode)
            parts = optimizer.global_stitch(frags)
            final_groups = optimizer.sort_by_tl_chain(parts)
            # --- 在 Streamlit 中呼叫 ---
            gcode_string = export_to_gcode(final_groups, optimizer.header, cut_f=feedrate)
            
            st.success("🎉 格式轉換成功！")
            
            # 6. 下載與預覽
            st.download_button(
                label="💾 下載 G-code 檔案",
                data=gcode_string,
                file_name=f"{uploaded_file.name.split('.')[0]}.gcode",
                mime="text/plain"
            )
            
            st.code(final_gcode[:1500]) # 顯示預覽
                    
        except Exception as e:
            st.error(f"❌ 轉換過程出錯：{e}")
            st.exception(e)