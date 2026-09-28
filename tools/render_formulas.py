"""把界面中声明的全部 LaTeX 公式预渲染为 SVG：assets/formulas/<哈希>.svg。

运行：python tools/render_formulas.py [--clean]
公式源写在各页面模块的 `Eq(...)` 中；本脚本导入这些模块收集公式，
用 matplotlib mathtext（STIX 字体、字形转路径）渲染，运行时只需 QtSvg。
笔色写成占位色，界面加载时替换成当前主题的正文色。
--clean 同时删除已不再被引用的旧 SVG。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
from matplotlib import mathtext, rc_context  # noqa: E402
from matplotlib.font_manager import FontProperties  # noqa: E402

from widgets.formula_view import PLACEHOLDER, formula_key, registered_formulas  # noqa: E402

# 声明了公式的模块；新增公式页面时加到这里
FORMULA_MODULES = (
    "pages.control_param_panels.panels",
    "pages.frequency_response_page",
    "pages.power_flow_page",
)
FONT_SIZE = 17                    # SVG 1 单位 = 1 px，按 17 px 字号排版
OUT_DIR = ROOT / "assets" / "formulas"


def render(latex: str) -> str:
    buffer = io.BytesIO()
    with rc_context({"mathtext.fontset": "stix", "svg.fonttype": "path",
                     "svg.hashsalt": formula_key(latex)}):
        mathtext.math_to_image(f"${latex}$", buffer, prop=FontProperties(size=FONT_SIZE),
                               dpi=72, format="svg", color=PLACEHOLDER)
    svg = buffer.getvalue().decode("utf-8")
    # 去掉带时间戳的元数据，保证重复渲染结果一致
    svg = re.sub(r"\s*<metadata>.*?</metadata>", "", svg, flags=re.S)
    # 去掉白色画布底，公式叠在界面的深色卡片上
    svg = re.sub(r'\s*<g id="patch_1">.*?</g>', "", svg, count=1, flags=re.S)
    return svg


def main(argv: list[str]) -> int:
    import importlib
    for name in FORMULA_MODULES:
        importlib.import_module(name)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wanted = set()
    written = 0
    for latex in registered_formulas():
        path = OUT_DIR / f"{formula_key(latex)}.svg"
        wanted.add(path.name)
        svg = render(latex)
        if not path.exists() or path.read_text(encoding="utf-8") != svg:
            path.write_text(svg, encoding="utf-8")
            written += 1
    stale = [p for p in OUT_DIR.glob("*.svg") if p.name not in wanted]
    if "--clean" in argv:
        for path in stale:
            path.unlink()
    print(f"{len(wanted)} 条公式，更新 {written} 个 SVG，"
          f"{'删除' if '--clean' in argv else '未引用'} {len(stale)} 个旧文件")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
