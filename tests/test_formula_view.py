import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import pages.control_param_panels.panels as panels
import pages.frequency_response_page  # noqa: F401 - 注册波特图页公式
import pages.power_flow_page  # noqa: F401 - 注册功率流页公式
from widgets.formula_view import (
    Eq, FormulaImage, FormulaSheet, PLACEHOLDER, formula_path, registered_formulas,
)


def _app():
    return QApplication.instance() or QApplication([])


def test_every_declared_formula_has_prerendered_svg():
    missing = [latex for latex in registered_formulas() if not formula_path(latex).exists()]
    assert not missing, "请运行 python tools/render_formulas.py：" + "; ".join(missing)


def test_formula_svg_is_recolored_and_scales_instead_of_wrapping():
    _app()
    latex = registered_formulas()[0]
    assert PLACEHOLDER in formula_path(latex).read_text(encoding="utf-8")
    image = FormulaImage(latex)
    assert image.rendered
    natural = image.sizeHint()
    assert image.heightForWidth(natural.width() * 2) == natural.height()
    # 宽度不够时整体缩小，高度随之变小（单行），而不是折成两行变高
    assert image.heightForWidth(natural.width() // 2) < natural.height()


def test_control_panels_show_formula_cards_without_rich_text_equations():
    _app()
    for cls in (panels.PIPanel, panels.PositionPanel, panels.OpenLoopPanel,
                panels.MPCPanel, panels.SensorlessPanel, panels.CurrentChoppingPanel,
                panels.AnglePositionPanel, panels.VoltageControlPanel):
        panel = cls()
        sheet = panel._formula
        assert isinstance(sheet, FormulaSheet)
        images = sheet.formula_images()
        assert images and all(image.rendered for image in images), cls.__name__


def test_mpc_formula_inserts_loop_structure_note():
    _app()
    panel = panels.MPCPanel()
    for type_index in range(panel.mpc_type.count()):
        panel.mpc_type.setCurrentIndex(type_index)
        for loop_index in range(panel.loop.count()):
            panel.loop.setCurrentIndex(loop_index)
            text = panel._formula.plain_text()
            assert "环路结构" in text
            assert panels._LOOP_NOTE not in panel._formula.blocks()
            assert any(isinstance(block, Eq) for block in panel._formula.blocks())
