import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager
from pages.power_flow_page import PowerFlowPage
from widgets.power_sankey import PowerSankey

_DRIVE = {"supply": 62.0, "loss_src": 1.5, "inv": 60.5, "brake": 0.0,
          "cu": 6.8, "em": 53.7, "fric": 47.0, "kinetic": 6.7}
_REGEN = {"supply": 0.0, "loss_src": 0.0, "inv": -38.0, "brake": 38.0,
          "cu": 4.1, "em": -42.1, "fric": 9.0, "kinetic": -51.1}


def _app():
    return QApplication.instance() or QApplication([])


def test_sankey_topology_conserves_power_at_each_node():
    nodes, links = PowerSankey._graph(_DRIVE)
    by_source = {}
    for link in links:
        by_source.setdefault(link.source, 0.0)
        by_source[link.source] += link.value
    assert by_source["supply"] == 62.0                     # 母线 + 内阻
    assert by_source["inv"] == _DRIVE["em"] + _DRIVE["cu"]
    assert by_source["em"] == _DRIVE["fric"] + _DRIVE["kinetic"]
    assert {node.key for node in nodes} >= {"bus", "brake", "cu", "kin"}


def test_sankey_bus_value_follows_energy_direction():
    assert PowerSankey._node_value("bus", _DRIVE) == 60.5
    assert PowerSankey._node_value("bus", _REGEN) == -38.0
    assert PowerSankey._node_value("kin", _REGEN) == -51.1


def test_sankey_renders_idle_drive_and_regen_states():
    _app()
    sankey = PowerSankey()
    sankey.resize(1000, 700)
    assert sankey.idle
    sankey.grab()                       # 待机骨架
    sankey.set_active(True)
    for powers in (_DRIVE, _REGEN):
        sankey.set_data(powers, 24.0, "brake" if powers is _REGEN else "normal")
        assert not sankey.idle
        sankey._tick()
        assert not sankey.grab().isNull()
    sankey.set_active(False)


def test_power_flow_page_uses_sankey_and_clears_on_disable():
    _app()
    page = PowerFlowPage(CommManager())
    assert isinstance(page._diagram, PowerSankey)
    page._chk_enabled.setChecked(True)
    page._diagram.set_data(_DRIVE, 24.0, "normal")
    page._chk_enabled.setChecked(False)
    assert page._diagram.idle
    page.close()
