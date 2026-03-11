import math
import re
from collections import defaultdict


class GCodeOptimizer:
    def __init__(self, stitch_tolerance=0.05):
        self.stitch_tolerance = stitch_tolerance
        self.machine_config = {"on_cmd": "M3", "off_cmd": "M5"}
        self.header = []

    def parse_to_fragments(self, gcode_text):
        lines = gcode_text.splitlines()
        fragments = []
        active_frag = {"points": [], "commands": []}
        cur_x, cur_y, cur_z = 0.0, 0.0, 0.0
        found_motion = False

        for line in lines:
            cmd = line.strip().upper()
            if not cmd or cmd.startswith(('(', ';')): continue

            if not found_motion and re.search(r'G(20|21|90|91|17)', cmd):
                self.header.append(line.strip())
                continue

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

            if (is_g0 or is_lift or is_off) and active_frag["points"]:
                fragments.append(active_frag)
                active_frag = {"points": [], "commands": []}

            if re.search(r'G[1-3]', cmd):
                if not active_frag["points"]:
                    active_frag["points"].append({"x": cur_x, "y": cur_y, "z": cur_z})
                active_frag["points"].append({"x": nx, "y": ny, "z": nz})
                active_frag["commands"].append(line.strip())

            cur_x, cur_y, cur_z = nx, ny, nz

        if active_frag["points"]: fragments.append(active_frag)
        return fragments

    def global_stitch(self, fragments):
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

                    if math.hypot(tail['x'] - t_head['x'], tail['y'] - t_head['y']) < self.stitch_tolerance:
                        active["points"] += target["points"][1:]
                        active["commands"] += target["commands"]
                        pool.pop(i); expanded = True; break
                    elif math.hypot(tail['x'] - t_tail['x'], tail['y'] - t_tail['y']) < self.stitch_tolerance:
                        active["points"] += target["points"][::-1][1:]
                        active["commands"] += target["commands"][::-1]
                        pool.pop(i); expanded = True; break

            pts = active["points"]
            xs, ys = [p['x'] for p in pts], [p['y'] for p in pts]
            active["bounds"] = {"minX": min(xs), "maxX": max(xs), "minY": min(ys), "maxY": max(ys)}
            active["isClosed"] = math.hypot(pts[0]['x'] - pts[-1]['x'],
                                            pts[0]['y'] - pts[-1]['y']) < self.stitch_tolerance
            parts.append(active)
        return parts

    def sort_by_tl_chain(self, parts):
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

        if not groups: return []
        sorted_groups = []
        unvisited = groups[:]

        def get_score(g):
            first = g['children'][0] if g['children'] else g['main']
            p = first['points'][0]
            return (p['y'] * 10) - p['x']

        start_idx = max(range(len(unvisited)), key=lambda i: get_score(unvisited[i]))
        current = unvisited.pop(start_idx)
        sorted_groups.append(current)
        cur_pos = current['main']['points'][-1]

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
    output = ["; Optimized G-code v4.3 (Python Version)"]

    if header:
        output.append("\n".join(header))
    else:
        output.append("G21 G90")

    p_num = 1
    on_cmd = "M3 S255"
    off_cmd = "M5"

    for group in optimized_groups:
        all_parts = group['children'] + [group['main']]

        for p in all_parts:
            output.append(f"\n; --- Part #{p_num} ({'Closed' if p['isClosed'] else 'Open'}) ---")
            p_num += 1

            start = p['points'][0]
            output.append(f"G0 X{start['x']:.3f} Y{start['y']:.3f} F{rapid_f}")
            output.append(on_cmd)

            for idx, cmd in enumerate(p['commands']):
                clean_cmd = re.sub(r'F[0-9.]+', '', cmd).strip()
                if idx == 0:
                    output.append(f"{clean_cmd} F{cut_f}")
                else:
                    output.append(clean_cmd)

            output.append(off_cmd)

    output.append("\n; --- Job Finished ---\nG0 X0 Y0\nM30")
    return "\n".join(output)
