import os
import socket
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from communications.base_comm import BaseComm
from communications.comm_manager import CommManager
from communications.native_transport import native_tcp_transport_available
from communications.protocol import encode_frame
from communications.protocol_v2 import (
    MessageType, V2Frame, decode_v2_frame, encode_v2_frame,
)
from communications.v2_virtual_device import V2VirtualDevice
from config.config import CMD_START
from PySide6.QtWidgets import QApplication
from pages.communication_page import CommunicationPage


class LoopbackV2Driver(BaseComm):
    """用真实send/recv边界连接v2虚拟设备，可模拟串口任意分包。"""

    name = "LoopbackV2"

    def __init__(self, device=None, split_size=0):
        self.device = device or V2VirtualDevice()
        self.split_size = split_size
        self._open = False
        self._now = 0.0
        self._chunks = []
        self._lock = threading.Lock()

    def open(self, **cfg):
        self._open = True
        return True

    def close(self):
        self._open = False

    def is_open(self):
        return self._open

    def send(self, data: bytes):
        if not self._open:
            raise RuntimeError("测试驱动未打开")
        responses = self.device.receive_bytes(data, self._now)
        self.queue(responses)
        return len(data)

    def recv(self, size=64, timeout=None):
        if not self._open:
            raise RuntimeError("测试驱动未打开")
        with self._lock:
            self._now += 0.05
            self._queue_unlocked(self.device.poll(self._now))
            if self._chunks:
                return self._chunks.pop(0)
        return b""

    def queue(self, frames):
        with self._lock:
            self._queue_unlocked(frames)

    def _queue_unlocked(self, frames):
        for frame in frames:
            if self.split_size:
                self._chunks.extend(
                    frame[i:i + self.split_size]
                    for i in range(0, len(frame), self.split_size))
            else:
                self._chunks.append(frame)


class QueueRxDriver(BaseComm):
    """只提供注入的 RX 字节，模拟以太网波形口。"""

    name = "QueueRx"

    def __init__(self):
        self._open = True
        self._chunks = []
        self._lock = threading.Lock()

    def open(self, **cfg):
        self._open = True
        return True

    def close(self):
        self._open = False

    def is_open(self):
        return self._open

    def send(self, data: bytes):
        return len(data)

    def recv(self, size=64, timeout=None):
        if not self._open:
            raise RuntimeError("测试驱动未打开")
        with self._lock:
            if self._chunks:
                return self._chunks.pop(0)
        return b""

    def push(self, data: bytes):
        with self._lock:
            self._chunks.append(data)


def _wait_until(predicate, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _app():
    return QApplication.instance() or QApplication([])


def test_真实v2任意分包握手命令ACK与遥测():
    driver = LoopbackV2Driver(split_size=3)
    comm = CommManager()

    assert comm.connect_negotiated_v2(
        "RS-485", driver=driver, handshake_timeout_s=0.5)
    status = comm.protocol_status()
    assert status["mode"] == "negotiated-v2"
    assert status["session_state"] == "ready"
    assert status["device_id"] == "VIRTUAL-MOTOR-001"

    # 异步真实链路发送只表示等待ACK，不能提前宣称启动成功。
    assert comm.send_frame(encode_frame(CMD_START, b"target=900")) is False
    assert _wait_until(lambda: comm.protocol_status()["last_result"] is not None)
    assert comm.protocol_status()["last_result"].success is True
    assert comm._motor_sim.enabled is False

    frames = driver.device.emit_telemetry({
        "speed_actual": 876.0, "speed_target": 900.0,
        "vdc": 48.3, "fault_code": 0,
    }, driver._now)
    driver.queue(frames)
    assert _wait_until(lambda: comm.latest_frame().speed_actual == 876.0)
    assert comm.latest_frame().data_source == "real"
    comm.disconnect()


def test_真实v2握手无响应失败且绝不降级v1():
    device = V2VirtualDevice()
    device.drop_next_response()
    driver = LoopbackV2Driver(device)
    comm = CommManager()

    assert not comm.connect_negotiated_v2(
        "RS-232", driver=driver, handshake_timeout_s=0.12)
    status = comm.protocol_status()
    assert status["mode"] == "negotiated-v2"
    assert status["session_state"] == "idle"
    assert "握手" in status["session_lost_reason"]
    assert comm.is_connected() is False
    assert driver.is_open() is False


def test_真实v2身份白名单匹配才允许连接():
    driver = LoopbackV2Driver()
    comm = CommManager()
    comm.configure_expected_device_identity(
        "VIRTUAL-MOTOR-001", "virtual", "sim-")

    assert comm.connect_negotiated_v2(
        "RS-485", driver=driver, handshake_timeout_s=0.5)
    status = comm.protocol_status()
    assert status["identity_policy_active"] is True
    assert status["identity_verified"] is True
    assert status["identity_mismatch_reason"] == ""
    comm.disconnect()


def test_真实v2身份不匹配拒绝连接且不降级():
    driver = LoopbackV2Driver()
    comm = CommManager()
    comm.configure_expected_device_identity("WRONG-CONTROLLER")

    assert not comm.connect_negotiated_v2(
        "RS-232", driver=driver, handshake_timeout_s=0.5)
    status = comm.protocol_status()
    assert status["mode"] == "negotiated-v2"
    assert status["session_state"] == "idle"
    assert "WRONG-CONTROLLER" in status["session_lost_reason"]
    assert status["identity_verified"] is False
    assert driver.is_open() is False


def test_已连接真实v2收紧白名单立即使会话失效():
    driver = LoopbackV2Driver()
    comm = CommManager()
    assert comm.connect_negotiated_v2(
        "RS-485", driver=driver, handshake_timeout_s=0.5)

    assert comm.configure_expected_device_identity("ANOTHER-BOARD") is False
    assert comm.is_connected() is False
    assert "ANOTHER-BOARD" in comm.protocol_status()["session_lost_reason"]
    comm.disconnect()


def test_真实v2运行中设备复位由心跳发现():
    driver = LoopbackV2Driver()
    comm = CommManager()
    assert comm.connect_negotiated_v2(
        "以太网TCP", driver=driver, handshake_timeout_s=0.5)

    driver.device.reboot()

    assert _wait_until(
        lambda: comm.protocol_status()["session_state"] == "idle", timeout=1.2)
    assert comm.is_connected() is False
    assert comm.protocol_status()["statistics"]["session_restarts_or_losses"] == 1
    assert "握手" in comm.protocol_status()["session_lost_reason"]
    comm.disconnect()


def test_真实v2心跳ACK丢失但遥测持续时不误判断链(monkeypatch):
    class DropHeartbeatAckDriver(LoopbackV2Driver):
        def send(self, data: bytes):
            frame = decode_v2_frame(data)
            responses = self.device.receive_bytes(data, self._now)
            if frame.message_type is not MessageType.HEARTBEAT:
                self.queue(responses)
            return len(data)

    monkeypatch.setattr(CommManager, "_V2_HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(CommManager, "_V2_HEARTBEAT_ACK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(CommManager, "_V2_CONTROL_RX_WARN_S", 0.15)
    monkeypatch.setattr(CommManager, "_V2_TELEMETRY_LIVENESS_S", 0.15)
    driver = DropHeartbeatAckDriver()
    telem = QueueRxDriver()
    comm = CommManager()

    assert comm.connect_negotiated_v2(
        "RS-485+以太网", driver=driver, telem_driver=telem,
        handshake_timeout_s=0.5)
    telemetry = driver.device.emit_telemetry(
        {"speed_actual": 500.0, "speed_target": 500.0}, driver._now)
    telem.push(telemetry[0])
    assert _wait_until(
        lambda: comm.protocol_status()["heartbeat_missing"],
        timeout=1.0)
    assert comm.protocol_status()["session_state"] == "ready"
    assert comm.is_connected() is True
    # 不拆协议会话，但必须把控制面无响应与遥测在线区分开。
    assert comm.protocol_status()["heartbeat_missing"] is True
    assert comm.latest_frame().speed_actual == 500.0
    comm.disconnect()


def test_真实v2心跳ACK丢失也不拆会话(monkeypatch):
    class DropHeartbeatAckDriver(LoopbackV2Driver):
        def send(self, data: bytes):
            frame = decode_v2_frame(data)
            responses = self.device.receive_bytes(data, self._now)
            if frame.message_type is not MessageType.HEARTBEAT:
                self.queue(responses)
            return len(data)

    monkeypatch.setattr(CommManager, "_V2_HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(CommManager, "_V2_HEARTBEAT_ACK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(CommManager, "_V2_CONTROL_RX_WARN_S", 0.15)
    driver = DropHeartbeatAckDriver()
    comm = CommManager()
    faults = []
    comm.faultDetected.connect(faults.append)

    assert comm.connect_negotiated_v2(
        "RS-485", driver=driver, handshake_timeout_s=0.5)
    time.sleep(0.5)
    status = comm.protocol_status()
    assert status["heartbeat_missing"] is True
    assert status["session_state"] == "ready"
    assert comm.is_connected() is True
    assert status["statistics"]["session_restarts_or_losses"] == 0
    assert faults == []
    comm.disconnect()


def test_以太网F1积压不能让串口心跳显示缺失(monkeypatch):
    monkeypatch.setattr(CommManager, "_V2_HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(CommManager, "_V2_HEARTBEAT_ACK_TIMEOUT_S", 0.25)
    monkeypatch.setattr(CommManager, "_TELEM_DRAIN_MAX_BYTES", 64)
    serial = LoopbackV2Driver()
    telem = QueueRxDriver()
    comm = CommManager()
    original = comm._process_v2_responses

    def slow_telem(responses, *, source="control"):
        frames = [decode_v2_frame(raw) for raw in responses]
        if any(frame.message_type is MessageType.TELEMETRY for frame in frames):
            time.sleep(0.12)
        return original(responses, source=source)

    comm._process_v2_responses = slow_telem
    assert comm.connect_negotiated_v2(
        "RS-485+以太网", driver=serial, telem_driver=telem,
        handshake_timeout_s=0.5)
    f1 = encode_v2_frame(V2Frame(
        MessageType.TELEMETRY, command=0xF1,
        payload=b"\x00" * 22, sequence=88))
    for _ in range(12):
        telem.push(f1)
    time.sleep(0.7)
    status = comm.protocol_status()
    assert status["session_state"] == "ready"
    assert status["heartbeat_missing"] is False
    comm.disconnect()


def test_RS485加以太网F1半包不能冲掉串口心跳ACK(monkeypatch):
    monkeypatch.setattr(CommManager, "_V2_HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(CommManager, "_V2_HEARTBEAT_ACK_TIMEOUT_S", 0.35)
    monkeypatch.setattr(CommManager, "_V2_TELEMETRY_LIVENESS_S", 0.2)
    serial = LoopbackV2Driver()
    telem = QueueRxDriver()
    comm = CommManager()

    assert comm.connect_negotiated_v2(
        "RS-485+以太网", driver=serial, telem_driver=telem,
        handshake_timeout_s=0.5)
    f1 = encode_v2_frame(V2Frame(
        MessageType.TELEMETRY, command=0xF1,
        payload=b"\x00" * 22, sequence=77))
    telem.push(f1[:20])
    time.sleep(0.12)
    telem.push(f1[20:])
    assert _wait_until(
        lambda: comm.protocol_status()["statistics"]["telemetry_frames"] >= 1,
        timeout=1.0)
    status = comm.protocol_status()
    assert status["session_state"] == "ready"
    assert comm.is_connected() is True
    assert status["statistics"]["crc_or_frame_errors"] == 0
    comm.disconnect()


@pytest.mark.skipif(
    not native_tcp_transport_available(), reason="尚未构建 C++ TCP 接收器")
def test_RS485加以太网自动使用Cpp线程接收F1(monkeypatch):
    monkeypatch.delenv("MOTOR_HMI_NATIVE_TRANSPORT", raising=False)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    release_server = threading.Event()
    f1 = encode_v2_frame(V2Frame(
        MessageType.TELEMETRY, command=0xF1,
        payload=b"\x00" * 22, sequence=77))

    def serve():
        try:
            connection, _address = listener.accept()
            with connection:
                connection.sendall(f1[:9])
                connection.sendall(f1[9:])
                release_server.wait(2.0)
        finally:
            listener.close()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    comm = CommManager()
    try:
        assert comm.connect_negotiated_v2(
            "RS-485+以太网", driver=LoopbackV2Driver(),
            host="127.0.0.1", tcp_port=port,
            local_host="127.0.0.1", tcp_timeout=1.0)
        assert comm.protocol_status()["telemetry_transport_backend"] == "cpp-tcp"
        assert _wait_until(
            lambda: comm.protocol_status()["statistics"]["telemetry_frames"] >= 1)
        native_stats = comm.protocol_status()["native_telemetry_transport"]
        assert native_stats["rx_frames"] >= 1
        assert native_stats["decoder_errors"] == 0
        parser_stats = comm.protocol_status()["native_telemetry_parser"]
        assert parser_stats["f1_frames"] >= 1
        assert parser_stats["f1_samples"] >= 1
    finally:
        comm.disconnect()
        release_server.set()
        server.join(timeout=1.0)


def test_会话失效不把物理链路标成断开():
    serial = LoopbackV2Driver()
    telem = QueueRxDriver()
    comm = CommManager()
    status_events = []
    faults = []
    comm.statusChanged.connect(lambda ok, msg: status_events.append((ok, msg)))
    comm.faultDetected.connect(faults.append)

    assert comm.connect_negotiated_v2(
        "RS-485+以太网", driver=serial, telem_driver=telem,
        handshake_timeout_s=0.5)
    after_connect = len(status_events)
    comm._handle_v2_session_loss("设备心跳ACK超时且遥测中断，会话已失效")

    assert serial.is_open() is True
    assert telem.is_open() is True
    assert comm.is_connected() is False
    assert all(ok for ok, _msg in status_events[after_connect:])
    assert faults and "会话失效" in faults[-1]
    comm.disconnect()


def test_真实v2拒绝经典CAN直到定义分片协议():
    comm = CommManager()

    assert not comm.connect_negotiated_v2("CAN总线", handshake_timeout_s=0.1)
    status = comm.protocol_status()
    assert status["mode"] == "negotiated-v2"
    assert "分片" in status["session_lost_reason"]
    assert comm.is_connected() is False


def test_真实v2短写被视为连接失败():
    class ShortWriteDriver(LoopbackV2Driver):
        def send(self, data):
            return len(data) - 1

    comm = CommManager()
    driver = ShortWriteDriver()

    assert not comm.connect_negotiated_v2(
        "RS-232", driver=driver, handshake_timeout_s=0.1)
    assert "未完整发送" in comm.protocol_status()["session_lost_reason"]


def test_通信页真实v2保留传输参数并调用严格握手(tmp_path, monkeypatch):
    app = _app()
    monkeypatch.setattr(
        "pages.communication_page._COMM_CFG_FILE", tmp_path / "comm.json")
    comm = CommManager()
    calls = []
    monkeypatch.setattr(
        comm, "connect_negotiated_v2",
        lambda kind, **cfg: calls.append((kind, cfg)) or True)
    page = CommunicationPage(comm)

    page._protocol_mode.setCurrentIndex(
        page._protocol_mode.findData("negotiated-v2"))
    assert page._kind.isEnabled() is True
    assert page._stack.isEnabled() is True
    assert "CAN" in page._status_label.toolTip()

    page._on_connect()
    assert calls and calls[0][0] == page._kind.currentText()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_辨识档开启F3且相电流保持链路默认全速(tmp_path, monkeypatch):
    app = _app()
    monkeypatch.setattr(
        "pages.communication_page._COMM_CFG_FILE", tmp_path / "comm.json")
    page = CommunicationPage(CommManager())

    page._tele_level.setCurrentIndex(page._tele_level.findData("id"))

    assert page._resolve_telemetry() == (0x03, 0, 20, 100)
    assert "相电流自动" in page._tele_hint.text()
    assert "辨识开" in page._tele_hint.text()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_通信配置保存并恢复辨识档(tmp_path, monkeypatch):
    app = _app()
    cfg_path = tmp_path / "comm.json"
    monkeypatch.setattr(
        "pages.communication_page._COMM_CFG_FILE", cfg_path)
    page = CommunicationPage(CommManager())
    page._tele_level.setCurrentIndex(page._tele_level.findData("id"))
    page._on_save_cfg()
    page.close()
    page.deleteLater()
    app.processEvents()

    restored = CommunicationPage(CommManager())
    assert restored._tele_level.currentData() == "id"
    assert restored._resolve_telemetry() == (0x03, 0, 20, 100)
    restored.close()
    restored.deleteLater()
    app.processEvents()
