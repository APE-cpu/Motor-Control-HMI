import os
import struct
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager
from communications.protocol import encode_frame
from communications.protocol_session import (
    DeviceCapabilities, ProtocolSession, ProtocolSessionState,
)
from communications.v2_virtual_device import V2VirtualDevice
from communications.protocol_v2 import MessageType, V2Frame, encode_v2_frame, make_ack
from config.config import CMD_SET_SENSOR, CMD_START, CMD_STOP
from pages.communication_page import CommunicationPage
from main_window import MainWindow
from core import RuntimeState


def _app():
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout=1.0):
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def test_真实v2不支持普通命令不会销毁会话():
    comm = CommManager()
    session = ProtocolSession()
    session.state = ProtocolSessionState.READY
    session.negotiated_version = 2
    session.capabilities = DeviceCapabilities(
        "DEVICE", "1.0", commands=[CMD_START])
    comm._protocol_mode = "negotiated-v2"
    comm._v2_session = session
    class OpenDriver:
        @staticmethod
        def is_open():
            return True
    comm._driver = OpenDriver()  # build_command 本地拒绝，不应触碰传输层
    disconnected = []
    comm.statusChanged.connect(lambda ok, msg: disconnected.append((ok, msg)))

    assert comm._send_negotiated_v2(
        encode_frame(CMD_SET_SENSOR, bytes([1]))) is False
    assert session.state is ProtocolSessionState.READY
    assert disconnected == []


def test_真实v2会话失效但物理链路仍可发送停止():
    comm = CommManager()
    sent = []

    class OpenDriver:
        @staticmethod
        def is_open():
            return True

        @staticmethod
        def send(data):
            sent.append(data)
            return len(data)

    session = ProtocolSession()  # IDLE，模拟心跳超时后的会话
    comm._protocol_mode = "negotiated-v2"
    comm._v2_session = session
    comm._driver = OpenDriver()

    assert comm._send_negotiated_v2(encode_frame(CMD_STOP)) is True
    assert sent


def test_CommManager_v2握手命令ACK和遥测闭环():
    comm = CommManager()
    telemetry_event = threading.Event()
    frames = []
    comm.telemetryReceived.connect(lambda frame: (frames.append(frame), telemetry_event.set()))

    assert comm.connect_virtual_v2() is True
    status = comm.protocol_status()
    assert status["mode"] == "virtual-v2"
    assert status["session_state"] == "ready"
    assert status["device_id"] == "VIRTUAL-MOTOR-001"
    assert comm.is_connected() is True
    assert comm.is_sim_running() is True

    sent = comm.send_frame(encode_frame(CMD_START, b"target=1200"))
    assert sent is True
    assert comm.protocol_status()["last_result"].success is True
    assert _wait_until(telemetry_event.is_set)
    assert frames[-1].data_source == "sim"
    comm.disconnect()


def test_200Hz高速通道解码Iq和两相电流():
    comm = CommManager()
    payload = struct.pack("<IHhhhhh", 1234, 32768, 100,
                          200, 80, 300, -150)
    raw = encode_v2_frame(V2Frame(
        MessageType.TELEMETRY, command=0xF1, payload=payload))

    samples = comm._process_v2_responses([raw])

    assert len(samples) == 1
    assert samples[0]["iq_a"] == 200 * 0.000629
    assert samples[0]["iqref_a"] == 80 * 0.000629
    assert samples[0]["ia_a"] == 300 * 0.000629
    assert samples[0]["ib_a"] == -150 * 0.000629


def test_1kHz以太网批量高速通道逐样本解码():
    comm = CommManager()
    batches = []
    comm.highRateTelemetryBatchReceived.connect(batches.append)
    one = struct.pack("<IHhhhhh", 100, 1000, 900, 200, 80, 300, -150)
    two = struct.pack("<IHhhhhh", 101, 1100, 901, 201, 81, 301, -151)
    raw = encode_v2_frame(V2Frame(
        MessageType.TELEMETRY, command=0xF1, payload=one + two))
    outputs = comm._process_v2_responses([raw])
    assert len(outputs) == 2
    assert len(batches) == 1
    samples = batches[0]
    assert [sample["tick_ms"] for sample in samples] == [100, 101]
    assert all(sample["rate_hz"] == 1000 for sample in samples)
    assert samples[1]["ib_a"] == -151 * 0.000629


def test_通信页静默接受1kHz批量F1且限制日志长度():
    _app()
    comm = CommManager()
    page = CommunicationPage(comm)
    sample = struct.pack("<IHhhhhh", 100, 1000, 900, 200, 80, 300, -150)
    wire = encode_v2_frame(V2Frame(
        MessageType.TELEMETRY, command=0xF1, payload=sample * 16))
    page._on_raw_received(0, wire)
    assert not any("高速诊断帧长度无效" in line for line in page._log_lines)
    for index in range(5100):
        page._append_log(f"[状态] test-{index}")
    assert len(page._log_lines) == 5000
    assert page._log.document().blockCount() <= 2000
    page.close()


def test_v2_NACK成为明确命令失败():
    comm = CommManager()
    device = V2VirtualDevice()
    assert comm.connect_virtual_v2(device)
    device.nack_next_command(205, "母线未预充")

    assert comm.send_frame(encode_frame(CMD_START)) is False
    result = comm.protocol_status()["last_result"]
    assert result.success is False
    assert result.error_code == 205
    assert result.message == "母线未预充"
    comm.disconnect()


def test_v2延迟应答会更新会话面板状态():
    comm = CommManager()
    device = V2VirtualDevice()
    assert comm.connect_virtual_v2(device)
    device.response_delay_s = 0.3
    results = []
    event = threading.Event()
    comm.commandResult.connect(lambda result: (results.append(result), event.set()))

    # 同步调用时尚无ACK，因此返回False；后台虚拟时钟随后收到迟到ACK。
    assert comm.send_frame(encode_frame(CMD_START)) is False
    assert comm.protocol_status()["pending_ack"] == 1
    assert _wait_until(event.is_set)
    assert results[-1].success is True
    assert comm.protocol_status()["pending_ack"] == 0
    comm.disconnect()


def test_v2故障遥测进入统一故障信号():
    comm = CommManager()
    device = V2VirtualDevice()
    assert comm.connect_virtual_v2(device)
    faults = []
    event = threading.Event()
    comm.faultDetected.connect(lambda reason: (faults.append(reason), event.set()))

    device.inject_fault(0x42, "模拟驱动故障", now_s=comm._v2_now)

    assert _wait_until(event.is_set)
    assert faults[-1] == "模拟驱动故障"
    assert comm.latest_frame().fault_code == 0x42
    comm.disconnect()


def test_通信页可选择virtual_v2并显示设备会话(tmp_path, monkeypatch):
    app = _app()
    monkeypatch.setattr("pages.communication_page._COMM_CFG_FILE",
                        tmp_path / "comm_config.json")
    comm = CommManager()
    page = CommunicationPage(comm)
    page._protocol_mode.setCurrentIndex(page._protocol_mode.findData("virtual-v2"))

    page._on_connect()

    assert page._session_state.text() == "READY（握手完成）"
    assert "VIRTUAL-MOTOR-001" in page._session_device.text()
    assert page._kind.isEnabled() is False
    page._on_disconnect()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_v2迟到ACK驱动全局启动和停机状态(tmp_path, monkeypatch):
    app = _app()
    monkeypatch.setattr(
        "pages.experiment_page.writable_path",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    window = MainWindow(enable_training=False)
    assert window.comm_manager.connect_virtual_v2()
    device = window.comm_manager.virtual_v2_device()
    device.response_delay_s = 0.3
    window.runtime_state.begin_precheck()
    window.runtime_state.pass_precheck()

    window.control_page._on_start()
    assert window.runtime_state.state is RuntimeState.READY
    assert _wait_until(
        lambda: window.runtime_state.state is RuntimeState.RUNNING, timeout=1.0)

    window.control_page._on_stop()
    assert window.runtime_state.state is RuntimeState.STOPPING
    assert _wait_until(
        lambda: window.runtime_state.state is RuntimeState.READY, timeout=1.0)
    window.close()
    window.deleteLater()
    app.processEvents()


def test_v2_START_NACK绝不把界面留在RUNNING(tmp_path, monkeypatch):
    app = _app()
    monkeypatch.setattr(
        "pages.experiment_page.writable_path",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    window = MainWindow(enable_training=False)
    assert window.comm_manager.connect_virtual_v2()
    window.runtime_state.begin_precheck()
    window.runtime_state.pass_precheck()
    window.comm_manager.virtual_v2_device().nack_next_command(
        102, "START interlock")

    window.control_page._on_start()

    assert window.runtime_state.state is RuntimeState.READY
    assert "启动失败" in window.statusBar().currentMessage()
    window.close()
    window.deleteLater()
    app.processEvents()


def test_v2停机NACK回到运行而ACK超时锁定故障(tmp_path, monkeypatch):
    app = _app()
    monkeypatch.setattr(
        "pages.experiment_page.writable_path",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    window = MainWindow(enable_training=False)
    assert window.comm_manager.connect_virtual_v2()
    device = window.comm_manager.virtual_v2_device()
    window.runtime_state.begin_precheck()
    window.runtime_state.pass_precheck()
    window.control_page._on_start()
    assert window.runtime_state.state is RuntimeState.RUNNING

    device.nack_next_command(301, "停机条件暂不满足")
    window.control_page._on_stop()
    assert window.runtime_state.state is RuntimeState.RUNNING

    # 与后台心跳共用协议锁，保证丢弃的是本次STOP响应而非心跳ACK。
    with window.comm_manager._v2_lock:
        device.drop_next_response()
        window.control_page._on_stop()
    assert window.runtime_state.state is RuntimeState.STOPPING
    assert _wait_until(
        lambda: window.runtime_state.state is RuntimeState.FAULT_LOCKED,
        timeout=1.5)
    window.close()
    window.deleteLater()
    app.processEvents()


def test_运行中设备重启被心跳发现并锁定状态机(tmp_path, monkeypatch):
    app = _app()
    monkeypatch.setattr(
        "pages.experiment_page.writable_path",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    window = MainWindow(enable_training=False)
    assert window.comm_manager.connect_virtual_v2()
    window.runtime_state.begin_precheck()
    window.runtime_state.pass_precheck()
    window.control_page._on_start()
    assert window.runtime_state.state is RuntimeState.RUNNING

    window.comm_manager.virtual_v2_device().reboot()

    assert _wait_until(
        lambda: (window.comm_manager.protocol_status()["session_state"] == "idle" and
                 window.runtime_state.state is RuntimeState.FAULT_LOCKED),
        timeout=1.2)
    assert window.comm_manager.is_connected() is False
    assert window.runtime_state.state is RuntimeState.FAULT_LOCKED
    status = window.comm_manager.protocol_status()
    assert "握手" in status["session_lost_reason"]
    assert status["pending_ack"] == 0
    assert status["statistics"]["session_restarts_or_losses"] == 1
    assert window.comm_manager.send_frame(encode_frame(CMD_START)) is False
    window.close()
    window.deleteLater()
    app.processEvents()


def test_v2协议统计覆盖收发错误与重复ACK():
    comm = CommManager()
    assert comm.connect_virtual_v2()
    assert comm.send_frame(encode_frame(CMD_START))
    result = comm.protocol_status()["last_result"]

    duplicate = encode_v2_frame(V2Frame(
        MessageType.ACK, command=result.command, sequence=result.sequence))
    comm._process_v2_responses([duplicate])
    corrupted = bytearray(duplicate)
    corrupted[-3] ^= 1
    comm._process_v2_responses([bytes(corrupted)])

    stats = comm.protocol_status()["statistics"]
    assert stats["handshakes"] == 1
    assert stats["tx_frames"] >= 2
    assert stats["rx_frames"] >= 4  # CAPABILITIES、ACK、重复ACK、坏帧
    assert stats["acks"] >= 1
    assert stats["late_or_duplicate_acks"] == 1
    assert stats["crc_or_frame_errors"] == 1
    comm.disconnect()
