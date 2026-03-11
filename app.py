import re

import streamlit as st
from svg_to_gcode.svg_parser import parse_string

from gcode.compiler import AdaptiveInterface, MySmartCompiler
from gcode.config import (DEFAULT, MACHINE_DATABASE, MATERIAL_DATABASE,
                          MM_TO_PX, ONE_BIT, PX_TO_MM, TOOL_DATABASE, WorkMode)
from gcode.image_pipeline import SVGConverter
from gcode.machine import calculate_cnc_params, get_dial
from gcode.optimizer import GCodeOptimizer, export_to_gcode
from gcode.svg_utils import filter_curves, get_svg_bbox

st.title("🎨 影像轉 G-code 產生器")
st.subheader("支援 JPG, PNG 向量化並統一輸出 G-code")


# --- 側邊欄 UI ---
with st.sidebar:
    st.header("🤖 機器配置")

    machine_name = st.selectbox("1. 選擇機器型號", list(MACHINE_DATABASE.keys()), key="machine_select")
    machine_info = MACHINE_DATABASE[machine_name]

    work_mode = None
    feedrate = 1500

    if machine_name != "請選擇機器...":
        st.info(f"📏 範圍: {machine_info['width']}x{machine_info['height']}x{machine_info['depth']} mm")

        work_mode = st.radio(
            "2. 選擇工作模式",
            machine_info["modes"],
            format_func=lambda x: x.value,
            key="mode_radio"
        )

        rotation_speed = 0
        if work_mode == WorkMode.CNC:
            st.subheader("🛠️ CNC 設定")
            tool_info = st.selectbox("選擇鑽頭", list(TOOL_DATABASE.keys()), key="tool_select")
            selected_material = st.selectbox("選擇材料", list(MATERIAL_DATABASE.keys()), key="material_select")

            rotation_speed, feedrate = calculate_cnc_params(tool_info, selected_material)

            st.success(f"📈 建議參數:\n- 主軸轉速: {rotation_speed} RPM (撥盤: {get_dial(rotation_speed)})\n- 進給速度: {feedrate} mm/min")

            feedrate = st.number_input("進給速度 (mm/min)", value=feedrate, step=50)
            rotation_speed = st.number_input("主軸轉速 (RPM)", value=rotation_speed, step=100)

        else:
            feedrate = 1500

        fill_on = False
        spacing = 0.05
        if work_mode == WorkMode.LASER or work_mode == WorkMode.PEN:
            st.subheader("🖼️ 轉檔設定")
            fill_on = st.sidebar.checkbox("啟用填滿", value=False)
            if fill_on:
                spacing = st.sidebar.number_input("填滿間隔 (mm)", min_value=0.02, max_value=10.0, value=0.05, step=0.01)

        svg_converter = SVGConverter(fill_on, spacing, 0)

    else:
        st.warning("請先選擇機器")


# --- 主畫面轉換邏輯 ---
uploaded_file = st.file_uploader("上傳圖片或 SVG", type=["jpg", "png", "svg"])
if uploaded_file and machine_name != "請選擇機器...":
    file_ext = uploaded_file.name.split('.')[-1].lower()

    if file_ext in ["jpg", "jpeg", "png"]:
        input_bytes = uploaded_file.getvalue()
        svg_raw = svg_converter.convert(input_bytes, DEFAULT)
    else:
        svg_raw = uploaded_file.getvalue().decode("utf-8")

    inner_svg_content = re.sub(r'<\?xml.*?\?>', '', svg_raw)
    inner_svg_content = re.sub(r'<svg[^>]*>', '', inner_svg_content)
    inner_svg_content = inner_svg_content.replace('</svg>', '')

    st.divider()
    st.subheader("🖼️ 圖稿位置與尺寸調整")

    m_w_mm, m_h_mm = machine_info["width"], machine_info["height"]
    m_w_half_mm, m_h_half_mm = m_w_mm / 2, m_h_mm / 2

    m_w_px = m_w_mm * MM_TO_PX
    m_h_px = m_h_mm * MM_TO_PX
    m_w_half_px, m_h_half_px = m_w_px / 2, m_h_px / 2

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

    auto_scale_calc = min(m_w_px * 0.8 / svg_width_px, m_h_px * 0.8 / svg_height_px) if svg_width_px > 0 else 1.0
    auto_width_mm = svg_width_px * auto_scale_calc * PX_TO_MM if svg_width_px > 0 else 100.0

    col_adj1, col_adj2, col_adj3 = st.columns(3)
    with col_adj1:
        width_mm = st.number_input("物件寬度 (mm)", min_value=1.0, max_value=float(m_w_mm), value=float(round(auto_width_mm, 1)), step=0.5)
    with col_adj2:
        off_x_mm = st.number_input("水平位移 (X mm)", min_value=-m_w_half_mm, max_value=m_w_half_mm, value=0.0, step=0.5)
    with col_adj3:
        off_y_mm = st.number_input("垂直位移 (Y mm)", min_value=-m_h_half_mm, max_value=m_h_half_mm, value=0.0, step=0.5)

    manual_scale = (width_mm / (svg_width_px * PX_TO_MM)) if svg_width_px > 0 else 1.0
    height_mm = svg_height_px * manual_scale * PX_TO_MM
    st.caption(f"計算高度: {height_mm:.1f} mm")

    off_x_px = off_x_mm * MM_TO_PX
    off_y_px = off_y_mm * MM_TO_PX

    auto_off_x_px = -(svg_width_px / 2)
    auto_off_y_px = -(svg_height_px / 2)
    bbox = get_svg_bbox(svg_raw)
    if bbox:
        xmin, ymin, xmax, ymax = bbox

        real_center_x = (xmin + xmax) / 2
        show_center_y = (ymin + ymax) / 2
        real_center_y = show_center_y - svg_height_px

        auto_off_x_px = -real_center_x
        show_center_y = -show_center_y
        auto_off_y_px = -real_center_y

        print(f"內容邊界: X({xmin} to {xmax}), Y({ymin} to {ymax})")
        print(f"建議偏移: X={auto_off_x_px}, Y={auto_off_y_px}")
    print(auto_off_x_px, auto_off_y_px)

    transform_str = f"translate({off_x_px}, {off_y_px}) scale({manual_scale}) translate({auto_off_x_px}, {show_center_y})"

    dynamic_font_size = int(m_w_px * 0.02)
    label_offset = int(m_w_px * 0.01)

    current_obj_w_mm = svg_width_px * manual_scale * PX_TO_MM
    current_obj_h_mm = svg_height_px * manual_scale * PX_TO_MM

    obj_center_x_px = off_x_mm * MM_TO_PX
    obj_center_y_px = off_y_mm * MM_TO_PX

    obj_w_px = (bbox[2] - bbox[0]) * manual_scale
    obj_h_px = (bbox[3] - bbox[1]) * manual_scale

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

    st.markdown("##### 預覽圖 (紅線為機器原點)")
    st.image(transformed_svg, use_container_width=True)

    st.download_button(
        label="📥 下載調整後的 SVG 檔案",
        data=convert_svg,
        file_name=f"{uploaded_file.name.split('.')[0]}_adjusted.svg",
        mime="image/svg+xml"
    )

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

            raw_gcode_body = compiler.compile()

            clean_lines = [line.strip() for line in raw_gcode_body.splitlines() if line.strip()]
            final_gcode = "\n".join(clean_lines)

            if fill_on:
                pattern = r"(M5;?\s+)G1(\s+[^M]+?)(?=\s+M3)"
                final_gcode = re.sub(pattern, r"\1G0\2", final_gcode, flags=re.MULTILINE)

                optimizer = GCodeOptimizer(stitch_tolerance=0.05)
                frags = optimizer.parse_to_fragments(final_gcode)
                parts = optimizer.global_stitch(frags)
                final_groups = optimizer.sort_by_tl_chain(parts)
                gcode_string = export_to_gcode(final_groups, optimizer.header, cut_f=feedrate)
            else:
                gcode_string = final_gcode

            st.success("🎉 格式轉換成功！")

            st.download_button(
                label="💾 下載 G-code 檔案",
                data=gcode_string,
                file_name=f"{uploaded_file.name.split('.')[0]}.gcode",
                mime="text/plain"
            )

            st.code(final_gcode[:1500])

        except Exception as e:
            st.error(f"❌ 轉換過程出錯：{e}")
            st.exception(e)
