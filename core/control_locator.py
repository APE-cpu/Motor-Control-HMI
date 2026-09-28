"""页面与控件定位：说明书卡片、新手引导和命令面板共用的界面索引。

控件 id 形如 ``monitor.probe_toggle``：前缀是页面键，后半段对应主窗口上的
属性路径。页面改版导致路径失效时，tests/test_manual_and_palette.py 会报错，
避免说明书指向一个已经不存在的按钮。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageInfo:
    key: str
    attr: str
    title: str
    aliases: tuple[str, ...] = ()


PAGES: tuple[PageInfo, ...] = (
    PageInfo("monitor", "monitor_page", "监控页面", ("监控", "monitor", "jk")),
    PageInfo("control", "control_page", "电机控制", ("控制", "参数", "control")),
    PageInfo("twin", "digital_twin_page", "数字孪生", ("孪生", "仿真", "twin")),
    PageInfo("experiment", "experiment_page", "实验管理", ("实验", "experiment")),
    PageInfo("vector", "vector_page", "矢量可视化", ("矢量", "电流圆", "vector")),
    PageInfo("power_flow", "power_flow_page", "功率流", ("功率", "power")),
    PageInfo("identify", "identify_page", "参数辨识", ("辨识", "identify")),
    PageInfo("fourier", "fourier_page", "离线傅里叶", ("傅里叶", "频谱", "fourier")),
    PageInfo("frequency_response", "frequency_response_page", "波特图与传函",
             ("波特图", "频响", "bode")),
    PageInfo("current_sampling", "current_sampling_page", "电流采样诊断",
             ("采样诊断", "adc")),
    PageInfo("ai", "ai_page", "诊断助手", ("ai", "助手")),
    PageInfo("edge_ai", "edge_ai_page", "边缘AI", ("边缘", "edge")),
    PageInfo("training", "training_page", "模型训练", ("训练", "training")),
    PageInfo("communication", "communication_page", "通信设置",
             ("通信", "连接", "comm")),
    PageInfo("operation_log", "operation_log_page", "操作记录", ("日志", "记录", "log")),
    PageInfo("manual", "manual_page", "使用说明书", ("说明书", "帮助", "manual")),
)

PAGE_BY_KEY = {page.key: page for page in PAGES}

# 控件 id → 主窗口上的属性路径（"()" 结尾表示调用无参方法）
CONTROLS: dict[str, str] = {
    "main.nav": "nav",
    "main.statusbar": "statusBar()",
    "main.appearance": "appearance_bar",
    "monitor.start": "monitor_page._btn_start",
    "monitor.stop": "monitor_page._btn_stop",
    "monitor.emergency": "monitor_page._btn_emerg",
    "monitor.speed": "monitor_page._speed_spin",
    "monitor.save_waves": "monitor_page._btn_save_all",
    "monitor.clear_waves": "monitor_page._btn_clear_curves",
    "monitor.probe_amplitude": "monitor_page._probe_amplitude",
    "monitor.probe_toggle": "monitor_page._btn_rls_probe",
    "monitor.rls_analyze": "monitor_page._btn_analyze_rls_capture",
    "communication.kind": "communication_page._kind",
    "communication.protocol": "communication_page._protocol_mode",
    "communication.connect": "communication_page._btn_connect",
    "communication.disconnect": "communication_page._btn_disconnect",
    "communication.telemetry_level": "communication_page._tele_level",
    "experiment.reset_fault": "experiment_page._btn_reset_fault",
    "ai.input": "ai_page._input",
    "control.max_rpm": "control_page._max_rpm",
    "control.current_limit": "control_page._current_limit",
    "control.apply": "control_page._btn_apply",
    "vector.enable": "vector_page._chk_enabled",
    "vector.source": "vector_page._cmb_source",
    "vector.shape": "vector_page._shape_label",
}


def page_for_control(control_id: str) -> PageInfo | None:
    return PAGE_BY_KEY.get(control_id.split(".", 1)[0])


def _walk(root, path: str):
    obj = root
    for part in path.split("."):
        if obj is None:
            return None
        if part.endswith("()"):
            method = getattr(obj, part[:-2], None)
            obj = method() if callable(method) else None
        else:
            obj = getattr(obj, part, None)
    return obj


def resolve_control(window, control_id: str):
    """返回控件对象；id 未登记或属性不存在时返回 None。"""
    path = CONTROLS.get(control_id)
    return _walk(window, path) if path else None


def resolve_page(window, page_key: str):
    page = PAGE_BY_KEY.get(page_key)
    return getattr(window, page.attr, None) if page else None


def current_page_key(window) -> str | None:
    current = window.stack.currentWidget()
    for page in PAGES:
        if getattr(window, page.attr, None) is current:
            return page.key
    return None


def show_page(window, page_key: str) -> bool:
    """通过左侧导航切换页面（与用户点击导航完全相同）。"""
    widget = resolve_page(window, page_key)
    if widget is None:
        return False
    index = window.stack.indexOf(widget)
    if index < 0:
        return False
    if window.stack.currentIndex() != index:
        window.nav.select_page(index)
    return True


def find_pages(query: str) -> list[PageInfo]:
    text = query.strip().lower()
    if not text:
        return []
    return [page for page in PAGES
            if text in page.title.lower() or text == page.key or
            any(text in alias.lower() for alias in page.aliases)]
