"""实验音视频记录：摄像头 + 麦克风录成 MP4（H.264/AAC），或只录音（M4A）。

文件放在实验目录的 media/ 下，media.json 记录每段录制的墙钟起止时间，以及相对
“实验开始”的秒数，实验日志据此把录像与数据时间对齐。
隐私：只有在本面板可见或正在录制时才打开摄像头；未勾选设备就不会采集。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import (
        QAudioInput, QCamera, QMediaCaptureSession, QMediaDevices, QMediaFormat, QMediaRecorder,
    )
    from PySide6.QtMultimediaWidgets import QVideoWidget
    MULTIMEDIA_OK = True
except ImportError:  # pragma: no cover - 精简打包可能不带多媒体模块
    MULTIMEDIA_OK = False

_NONE = "__none__"
VIDEO_BITRATE = 4_000_000


def _iso_now() -> str:
    # 与实验会话时间一致：带时区的本地时间（会话记录形如 2026-09-28T23:05:46.817+08:00）
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _parse_time(text: str) -> datetime:
    """统一成带时区的时间；无时区的旧记录按本地时间理解。"""
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo is not None else moment.astimezone()


def _offset_s(started_at: str | None, moment: str) -> float | None:
    if not started_at:
        return None
    try:
        return (_parse_time(moment) - _parse_time(started_at)).total_seconds()
    except (TypeError, ValueError):
        return None


def append_media_manifest(directory: Path, entry: dict) -> None:
    path = directory / "media.json"
    try:
        items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError):
        items = []
    items.append(entry)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def read_media_manifest(directory: Path) -> list[dict]:
    path = Path(directory) / "media.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError):
        return []


class MediaRecorder(QObject):
    """包装 QMediaCaptureSession；一次只录一段。"""

    recordingChanged = Signal(bool)
    errorOccurred = Signal(str)
    finished = Signal(dict)            # 一段录制的清单条目

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._session = self._camera = self._audio = self._recorder = None
        self._camera_id = self._mic_id = None
        self._entry: dict | None = None
        self._directory: Path | None = None
        if MULTIMEDIA_OK:
            self._session = QMediaCaptureSession(self)
            self._recorder = QMediaRecorder(self)
            self._session.setRecorder(self._recorder)
            self._recorder.errorOccurred.connect(
                lambda _error, text: self.errorOccurred.emit(text or "录制失败"))

    @staticmethod
    def cameras() -> list[tuple[str, str]]:
        if not MULTIMEDIA_OK:
            return []
        return [(bytes(d.id()).hex(), d.description()) for d in QMediaDevices.videoInputs()]

    @staticmethod
    def microphones() -> list[tuple[str, str]]:
        if not MULTIMEDIA_OK:
            return []
        return [(bytes(d.id()).hex(), d.description()) for d in QMediaDevices.audioInputs()]

    @property
    def available(self) -> bool:
        return MULTIMEDIA_OK

    @property
    def recording(self) -> bool:
        return self._entry is not None

    def set_video_output(self, widget) -> None:
        if self._session is not None:
            self._session.setVideoOutput(widget)

    def configure(self, camera_id: str | None, mic_id: str | None) -> None:
        """选择设备；None 表示不用。录制中不允许切换。"""
        if not MULTIMEDIA_OK or self.recording:
            return
        self._camera_id, self._mic_id = camera_id, mic_id
        if self._camera is not None:
            self._camera.stop()
            self._camera.deleteLater()
            self._camera = None
        if camera_id:
            device = next((d for d in QMediaDevices.videoInputs()
                           if bytes(d.id()).hex() == camera_id), None)
            if device is not None:
                self._camera = QCamera(device, self)
                self._camera.errorOccurred.connect(
                    lambda _error, text: self.errorOccurred.emit(text or "摄像头错误"))
        self._session.setCamera(self._camera)
        if self._audio is not None:
            self._audio.deleteLater()
            self._audio = None
        if mic_id:
            device = next((d for d in QMediaDevices.audioInputs()
                           if bytes(d.id()).hex() == mic_id), None)
            if device is not None:
                self._audio = QAudioInput(device, self)
        self._session.setAudioInput(self._audio)

    def set_preview(self, on: bool) -> None:
        if self._camera is not None and not self.recording:
            (self._camera.start if on else self._camera.stop)()

    def start(self, directory: Path, session_started_at: str | None = None,
              label: str = "录像") -> Path | None:
        if not MULTIMEDIA_OK or self.recording:
            return None
        if self._camera is None and self._audio is None:
            self.errorOccurred.emit("没有选择摄像头或麦克风")
            return None
        directory = Path(directory) / "media"
        directory.mkdir(parents=True, exist_ok=True)
        video = self._camera is not None
        stamp = datetime.now().strftime("%H%M%S")
        path = directory / (f"{label}_{stamp}.mp4" if video else f"录音_{stamp}.m4a")
        fmt = QMediaFormat(QMediaFormat.MPEG4 if video else QMediaFormat.Mpeg4Audio)
        if video:
            fmt.setVideoCodec(QMediaFormat.VideoCodec.H264)
        if self._audio is not None:
            fmt.setAudioCodec(QMediaFormat.AudioCodec.AAC)
        self._recorder.setMediaFormat(fmt)
        self._recorder.setEncodingMode(QMediaRecorder.AverageBitRateEncoding)
        # 默认质量在本机约 35 Mbit/s（52 分钟 13.8 GB）；实验录像限制为 720p、约 4 Mbit/s（≈1.8 GB/h）
        self._recorder.setVideoResolution(1280, 720)
        self._recorder.setVideoFrameRate(25.0)
        self._recorder.setVideoBitRate(VIDEO_BITRATE)
        self._recorder.setAudioBitRate(128_000)
        self._recorder.setOutputLocation(QUrl.fromLocalFile(str(path)))
        # 先把清单条目准备好，最后一步才开始录制：任何一步出错都不会留下“在录但没人管”的录像
        started = _iso_now()
        entry = {
            "file": path.name, "kind": "video" if video else "audio",
            "camera": self._device_name(self.cameras(), self._camera_id),
            "microphone": self._device_name(self.microphones(), self._mic_id),
            "started_at": started, "session_started_at": session_started_at,
            "start_offset_s": _offset_s(session_started_at, started),
        }
        try:
            if self._camera is not None:
                self._camera.start()
            self._recorder.record()
        except Exception as exc:  # noqa: BLE001 - 录像失败不能影响实验
            self._force_stop()
            self.errorOccurred.emit(f"录制启动失败：{exc}")
            return None
        self._directory = directory
        self._entry = entry
        self.recordingChanged.emit(True)
        return path

    def _force_stop(self) -> None:
        """不论清单状态如何，确保录制器和摄像头真的停下。"""
        if self._recorder is not None and \
                self._recorder.recorderState() != QMediaRecorder.StoppedState:
            self._recorder.stop()

    def stop(self) -> dict | None:
        if not self.recording:
            self._force_stop()
            return None
        self._force_stop()
        entry, self._entry = self._entry, None
        entry["ended_at"] = _iso_now()
        entry["end_offset_s"] = _offset_s(entry.get("session_started_at"), entry["ended_at"])
        entry["duration_s"] = _offset_s(entry["started_at"], entry["ended_at"])
        try:
            append_media_manifest(self._directory, entry)
        except OSError as exc:
            self.errorOccurred.emit(f"录像清单写入失败：{exc}")
        self.recordingChanged.emit(False)
        self.finished.emit(entry)
        return entry

    @staticmethod
    def _device_name(devices, device_id) -> str:
        return next((name for key, name in devices if key == device_id), "")


class MediaPanel(QWidget):
    """实验管理页的“音视频”标签：选设备、自动录制开关、预览、手动开始/停止。"""

    recordingChanged = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.recorder = MediaRecorder(self)
        self._settings = QSettings("MotorControlHMI", "media")
        self._session_dir: Path | None = None
        self._session_started_at: str | None = None
        root = QVBoxLayout(self)
        if not self.recorder.available:
            root.addWidget(QLabel("当前运行环境没有 Qt 多媒体模块，无法录像/录音。"))
            root.addStretch(1)
            return
        form = QFormLayout()
        self._camera = QComboBox()
        self._mic = QComboBox()
        # 枚举摄像头要约 0.9 s（Windows Media Foundation）：先只放上次选的设备，
        # 本页第一次显示或开始录制时再枚举真实设备（_ensure_devices）
        self._devices_loaded = False
        for combo, key, none_text in ((self._camera, "camera", "不录像"),
                                      (self._mic, "microphone", "不录音")):
            combo.addItem(none_text, _NONE)
            saved = self._settings.value(key, _NONE)
            if saved not in (None, _NONE):
                combo.addItem(self._settings.value(f"{key}_name", "上次选择的设备"), saved)
                combo.setCurrentIndex(1)
            combo.currentIndexChanged.connect(self._apply_devices)
        self.auto = QCheckBox("点“开始记录实验”时自动开始录制，结束或中止实验时自动停止")
        self.auto.setChecked(self._settings.value("auto", False, type=bool))
        self.auto.toggled.connect(lambda on: self._settings.setValue("auto", on))
        form.addRow("摄像头", self._camera)
        form.addRow("麦克风", self._mic)
        root.addLayout(form)
        root.addWidget(self.auto)
        self.preview = QVideoWidget()
        self.preview.setMinimumSize(320, 200)
        self.recorder.set_video_output(self.preview)
        root.addWidget(self.preview, 1)
        row = QHBoxLayout()
        self.btn_start = QPushButton("开始录制")
        self.btn_stop = QPushButton("停止录制")
        self.btn_start.clicked.connect(self.start_manual)
        self.btn_stop.clicked.connect(self.stop)
        self.status = QLabel("未录制")
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addWidget(self.status, 1)
        root.addLayout(row)
        note = QLabel("录像存到当前实验目录的 media/；没有进行中的实验时手动录制不可用。"
                      "只有本页可见或正在录制时摄像头才会打开。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#90a4ae; font-size:12px;")
        root.addWidget(note)
        self.recorder.recordingChanged.connect(self._on_recording)
        self.recorder.errorOccurred.connect(lambda text: self.status.setText(f"录制错误：{text}"))
        self._on_recording(False)

    # ------------------------------------------------------------ 设备
    def _selected(self, combo) -> str | None:
        value = combo.currentData()
        return None if value in (None, _NONE) else value

    def _ensure_devices(self) -> None:
        """第一次需要时枚举真实设备，保留原来的选择。"""
        if self._devices_loaded or not self.recorder.available:
            return
        self._devices_loaded = True
        for combo, devices in ((self._camera, self.recorder.cameras()),
                               (self._mic, self.recorder.microphones())):
            current = combo.currentData()
            combo.blockSignals(True)
            while combo.count() > 1:
                combo.removeItem(1)
            for key, name in devices:
                combo.addItem(name, key)
            combo.setCurrentIndex(max(0, combo.findData(current)))
            combo.blockSignals(False)
        self._apply_devices()

    def _apply_devices(self, *_args) -> None:
        if not self._devices_loaded:            # 用户在枚举前就改了选择
            self._ensure_devices()
            return
        for combo, key in ((self._camera, "camera"), (self._mic, "microphone")):
            self._settings.setValue(key, combo.currentData())
            self._settings.setValue(f"{key}_name", combo.currentText())
        self.recorder.configure(self._selected(self._camera), self._selected(self._mic))
        self.recorder.set_preview(self.isVisible())
        self._on_recording(self.recorder.recording)

    @property
    def has_device(self) -> bool:
        return self.recorder.available and bool(
            self._selected(self._camera) or self._selected(self._mic))

    # ------------------------------------------------------------ 实验联动
    def set_session(self, session_dir: Path | None, started_at: str | None) -> None:
        self._session_dir = Path(session_dir) if session_dir else None
        self._session_started_at = started_at
        self._on_recording(self.recorder.recording)

    @property
    def auto_requested(self) -> bool:
        return self.recorder.available and self.auto.isChecked()

    def start_for_session(self, session_dir: Path, started_at: str | None) -> Path | None:
        self.set_session(session_dir, started_at)
        if not self.auto_requested:
            return None
        if not self.has_device:
            self.status.setText("已勾选自动录制，但没有选择摄像头或麦克风")
            return None
        self._ensure_devices()
        return self.recorder.start(session_dir, started_at)

    def start_manual(self) -> None:
        if self._session_dir is not None:
            self._ensure_devices()
            self.recorder.start(self._session_dir, self._session_started_at)

    def stop(self) -> dict | None:
        return self.recorder.stop() if self.recorder.available else None

    def _on_recording(self, recording: bool) -> None:
        if not self.recorder.available:
            return
        self.btn_start.setEnabled(not recording and self._session_dir is not None
                                  and self.has_device)
        self.btn_stop.setEnabled(recording)
        for combo in (self._camera, self._mic):
            combo.setEnabled(not recording)
        self.status.setText("● 录制中" if recording else
                            ("未录制" if self._session_dir else "未录制（没有进行中的实验）"))
        self.status.setStyleSheet("color:#ef5350; font-weight:bold;" if recording else "")
        self.recordingChanged.emit(recording)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if self.recorder.available:
            self._ensure_devices()
            self.recorder.set_preview(True)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.recorder.available and not self.recorder.recording:
            self.recorder.set_preview(False)
        super().hideEvent(event)
