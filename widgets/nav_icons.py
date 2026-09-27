"""统一的导航线条图标；颜色随主题切换，SVG 按高 DPI 渲染。"""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


NAV_PATHS = {
    "监控页面": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M6 11h3l2-4 3 7 2-3h2M8 21h8M12 17v4"/>',
    "电机控制": '<path d="M5 3v5m0 4v9M12 3v10m0 4v4M19 3v2m0 4v12"/><rect x="3" y="8" width="4" height="4" rx="1"/><rect x="10" y="13" width="4" height="4" rx="1"/><rect x="17" y="5" width="4" height="4" rx="1"/>',
    "数字孪生": '<path d="m12 2 9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10M7.5 4.5l9 5v10M7.5 9.5v10M3 12l9 5 9-5"/>',
    "实验管理": '<path d="M8 3h8M10 3v7L4 19a1.3 1.3 0 0 0 1 2h14a1.3 1.3 0 0 0 1-2l-6-9V3M7 15h10"/><circle cx="10" cy="18" r=".7"/>',
    "矢量可视化": '<circle cx="12" cy="12" r="8"/><path d="M2 12h20M12 2v20M12 12l6-6m-4 0h4v4"/>',
    "功率流": '<path d="m13 2-8 11h6l-1 9 9-12h-6z"/>',
    "参数辨识": '<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6M6 10h2l2-3 2 6 2-3"/>',
    "离线傅里叶": '<path d="M3 3v18h18M6 18v-3M9 18V7M12 18v-5M15 18V4M18 18v-8M21 18v-3"/>',
    "波特图与传函": '<path d="M3 3v18h18M5 7h5c3 0 3 7 9 9M5 16c5 0 5-7 14-7"/>',
    "电流采样诊断": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M4 14h4V8h5v8h4v-5h3"/>',
    "诊断助手": '<path d="M20 15a3 3 0 0 1-3 3H9l-5 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3zM7 11h3l2-4 2 7 2-3h2"/>',
    "边缘AI": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M8 2v3m8-3v3M8 19v3m8-3v3M2 8h3m-3 8h3M19 8h3m-3 8h3"/>',
    "模型训练": '<circle cx="5" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="m6.5 6.5 4 4m3 3 4 4m-11 0 4-4m3-3 4-4"/>',
    "通信设置": '<path d="M9 2v5m6-5v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5"/>',
    "操作记录": '<path d="M14 2H5v20h14V7zM14 2v5h5M8 11h8M8 15h8M8 19h5"/>',
    "使用说明书": '<path d="M12 5v16M12 5C9 3 5 3 2 4v15c3-1 7-1 10 2 3-3 7-3 10-2V4c-3-1-7-1-10 1zM5 8h4M5 12h4M15 8h4M15 12h4"/>',
}


def navigation_icon(label, theme):
    paths = NAV_PATHS.get(label, '<circle cx="12" cy="12" r="7"/>')
    icon = QIcon()
    for mode, color in ((QIcon.Normal, theme.muted),
                        (QIcon.Active, theme.text),
                        (QIcon.Selected, theme.accent),
                        (QIcon.Disabled, theme.border)):
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
               f'<g fill="none" stroke="{color}" stroke-width="1.65" '
               f'stroke-linecap="round" stroke-linejoin="round">{paths}</g></svg>')
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(2.0)
        icon.addPixmap(pixmap, mode)
    return icon
