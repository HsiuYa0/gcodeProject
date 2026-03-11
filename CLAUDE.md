# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
streamlit run app.py --server.enableCORS false --server.enableXsrfProtection false
```

Open at **http://localhost:8501**.

## Docker

```bash
docker compose up --build   # build and run
docker compose down         # stop
```

## Python Environment

Dependencies are installed via pip. The correct Python is `/opt/homebrew/bin/python3.11` (all packages live there). Verify imports work with:

```bash
/opt/homebrew/bin/python3.11 -c "from gcode import config, machine, image_pipeline, svg_utils, compiler, optimizer"
```

## Architecture

This is a Streamlit web app that converts raster images and SVGs into machine-ready G-code for CNC/laser/pen plotters.

**Entry point:** `app.py` — contains all Streamlit UI code (sidebar config, file upload, preview, G-code export).

**`gcode/` package:**

| Module | Responsibility |
|--------|---------------|
| `config.py` | Constants (`MM_TO_PX`, `PX_TO_MM`), enums (`WorkMode`, `ToolHeadShape`), and static databases (`MACHINE_DATABASE`, `TOOL_DATABASE`, `MATERIAL_DATABASE`) |
| `machine.py` | `calculate_cnc_params(tool, material)` → `(rpm, feedrate)` and `get_dial(rpm)` for Makita RT0700 spindle |
| `image_pipeline.py` | Image → SVG conversion. Three pipelines: `VTracerBinaryPipeline` (vtracer + optional hatch fill), `OneBitPipeline` (threshold), `SkeletonPipeline` (skeletonize). `SVGConverter` is the factory. |
| `svg_utils.py` | `get_svg_bbox()` and `filter_curves()` — helpers used between SVG parsing and G-code compilation |
| `compiler.py` | `MySmartCompiler` (subclasses `svg_to_gcode` Compiler) and `AdaptiveInterface` (subclasses `svg_to_gcode` Gcode interface, handles LASER/CNC/PEN header/footer) |
| `optimizer.py` | `GCodeOptimizer`: parse raw G-code → fragments → stitch into parts → TL-Chain sort. `export_to_gcode()` serializes optimized groups back to a G-code string. |

**Data flow:**
```
Image/SVG upload
  → image_pipeline (image → SVG)
  → app.py: scale/offset UI, preview SVG rendered in browser
  → svg_utils.filter_curves(parse_string(svg))
  → compiler.MySmartCompiler.compile() → raw G-code
  → [optional] optimizer.GCodeOptimizer → optimized G-code
  → download
```

**Unit system:** All internal preview coordinates are in SVG pixels. Physical dimensions use mm. Conversion constants `MM_TO_PX = 96/25.4` and `PX_TO_MM = 25.4/96` are in `gcode/config.py`. The SVG viewBox is centered at (0,0) to match the machine's physical origin.

**Supported machine:** GORDIX (2280×1060×35 mm). Add new machines to `MACHINE_DATABASE` in `gcode/config.py`.
