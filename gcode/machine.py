import math

import numpy as np

from .config import TOOL_DATABASE, MATERIAL_DATABASE


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

    target_vc = (material["vc"][0] + material["vc"][1]) / 2
    rpm = (target_vc * 1000) / (np.pi * tool["diameter"])

    rpm = max(10000, min(30000, rpm))

    feedrate = rpm * tool.get("flutes", 1) * material["fz"]
    feedrate = min(2000, feedrate)

    return int(rpm), int(feedrate)
