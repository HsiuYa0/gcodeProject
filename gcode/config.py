from enum import Enum

MM_TO_PX = 96.0 / 25.4
PX_TO_MM = 25.4 / 96.0

ONE_BIT = "黑白預處理 (One-bit)"
DEFAULT = "快速掃描 (Quick)"


class WorkMode(Enum):
    LASER = "Laser"
    CNC = "CNC"
    PEN = "Pen"


class ToolHeadShape(Enum):
    TAPER_TIP = "taper_tip"
    FLAT_END = "flat_end"
    BALL_END = "ball_end"


MACHINE_DATABASE = {
    "請選擇機器...": {"width": 0, "height": 0, "depth": 0, "modes": [], "default_power": 0},
    "GORDIX": {
        "width": 2280, "height": 1060, "depth": 35, "modes": [WorkMode.LASER, WorkMode.CNC, WorkMode.PEN], "default_power": 255
    }
}

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
