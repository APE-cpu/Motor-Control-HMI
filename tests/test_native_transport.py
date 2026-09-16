import socket
import struct
import threading
import time

import pytest

from communications.comm_manager import CommManager
from communications.native_transport import (
    NativeTcpV2Receiver, native_tcp_transport_available,
    native_tcp_transport_enabled,
)
from communications.protocol_v2 import MessageType, V2Frame, encode_v2_frame


def _start_server(chunks: list[bytes]):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        try:
            connection, _address = listener.accept()
            with connection:
                for chunk in chunks:
                    connection.sendall(chunk)
                    time.sleep(0.002)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_可通过环境变量关闭原生TCP(monkeypatch):
    monkeypatch.setenv("MOTOR_HMI_NATIVE_TRANSPORT", "python")
    assert native_tcp_transport_enabled() is False


def test_对端关闭后先排空原生队列再释放接收器(monkeypatch):
    class ClosedReceiver:
        backend = "cpp-tcp"

        def __init__(self):
            self.frames = [
                V2Frame(MessageType.HEARTBEAT, sequence=index)
                for index in range(5)
            ]
            self.stopped = False

        def drain(self, max_frames):
            batch = self.frames[:max_frames]
            del self.frames[:max_frames]
            return batch

        def drain_f1(self, max_samples):
            return []

        def drain_f1_columns(self, max_samples):
            return {"count": 0}

        def drain_f2(self, max_samples):
            return []

        def drain_f3(self, max_samples):
            return []

        def drain_bursts(self, max_bursts):
            return []

        def stats(self):
            return {
                "running": False, "rx_bytes": 0, "rx_frames": 5,
                "dropped_frames": 0, "decoder_errors": 0,
                "queued_frames": len(self.frames),
                "telemetry": {
                    "f1_frames": 0, "f4_frames": 0, "f1_samples": 0,
                    "completed_bursts": 0, "parse_errors": 0,
                    "dropped_samples": 0, "dropped_bursts": 0,
                    "queued_samples": 0, "queued_bursts": 0,
                },
                "last_error": "peer closed",
            }

        def stop(self):
            self.stopped = True

    comm = CommManager()
    receiver = ClosedReceiver()
    comm._native_telem_receiver = receiver
    monkeypatch.setattr(comm, "_NATIVE_TELEM_DRAIN_MAX_FRAMES", 2)

    assert len(comm._drain_native_telem()[0]) == 2
    assert comm._native_telem_receiver is receiver
    assert len(comm._drain_native_telem()[0]) == 2
    assert comm._native_telem_receiver is receiver
    assert len(comm._drain_native_telem()[0]) == 1
    assert comm._native_telem_receiver is None
    assert receiver.stopped is True


@pytest.mark.skipif(
    not native_tcp_transport_available(), reason="尚未构建 C++ TCP 接收器")
def test_Cpp线程接收分包粘包并批量取帧():
    frames = [
        V2Frame(MessageType.TELEMETRY, 0xF1, bytes([index]) * 22,
                sequence=index)
        for index in range(40)
    ]
    wire = b"noise" + b"".join(map(encode_v2_frame, frames))
    chunks = [wire[:7], wire[7:101], wire[101:999], wire[999:]]
    port, server = _start_server(chunks)
    receiver = NativeTcpV2Receiver()
    try:
        receiver.start(
            "127.0.0.1", port, local_host="127.0.0.1", timeout_s=1.0)
        assert _wait_until(
            lambda: receiver.stats()["telemetry"]["f1_frames"] == len(frames))
        actual = receiver.drain_f1(100)
        stats = receiver.stats()
    finally:
        receiver.stop()
        server.join(timeout=1.0)

    assert [sample["tick_ms"] for sample in actual] == [
        int.from_bytes(frame.payload[:4], "little") for frame in frames]
    assert receiver.drain(100) == []
    assert stats["decoder_errors"] == 0
    assert stats["dropped_frames"] == 0
    assert stats["telemetry"]["f1_frames"] == len(frames)


@pytest.mark.skipif(
    not native_tcp_transport_available(), reason="尚未构建 C++ TCP 接收器")
def test_Cpp接收线程直接分流F2_F3_F4():
    f2_payload = struct.pack(
        "<IHHHHBHHHH", 20, 100, 200, 300, 400, 2, 10, 11, 12, 13)
    theta_d = [0.9, 0.0, 0.0, 0.05, 0.0, 0.0, 0.0]
    theta_q = [0.8, 0.0, 0.0, 0.0, 0.0, 0.04, 0.0]
    f3_payload = struct.pack(
        "<IIff14f", 21, 22, 0.1, 0.2, *(theta_d + theta_q))
    f4_payload = struct.pack("<HHHhhH", 1, 0, 1, 7, -8, 9)
    frames = [
        V2Frame(MessageType.TELEMETRY, 0xF2, f2_payload, sequence=1),
        V2Frame(MessageType.TELEMETRY, 0xF3, f3_payload, sequence=2),
        V2Frame(MessageType.TELEMETRY, 0xF4, f4_payload, sequence=3),
    ]
    port, server = _start_server([b"".join(map(encode_v2_frame, frames))])
    receiver = NativeTcpV2Receiver()
    receiver.set_rls_coefficients_si(True)
    try:
        receiver.start(
            "127.0.0.1", port, local_host="127.0.0.1", timeout_s=1.0)
        assert _wait_until(
            lambda: receiver.stats()["telemetry"]["completed_bursts"] == 1)
        f2_samples = receiver.drain_f2()
        f3_samples = receiver.drain_f3()
        bursts = receiver.drain_bursts()
        stats = receiver.stats()["telemetry"]
    finally:
        receiver.stop()
        server.join(timeout=1.0)

    assert f2_samples[0]["sector"] == 2
    assert f3_samples[0]["b_dd0_si"] == pytest.approx(0.05)
    assert bursts[0]["ia"] == [7]
    assert (stats["f2_frames"], stats["f3_frames"], stats["f4_frames"]) == (
        1, 1, 1)


@pytest.mark.skipif(
    not native_tcp_transport_available(), reason="尚未构建 C++ TCP 接收器")
def test_Cpp有界队列过载时丢旧帧保留最新帧():
    frames = [
        V2Frame(MessageType.HEARTBEAT, sequence=index)
        for index in range(10)
    ]
    port, server = _start_server([b"".join(map(encode_v2_frame, frames))])
    receiver = NativeTcpV2Receiver(max_queue_frames=3)
    try:
        receiver.start(
            "127.0.0.1", port, local_host="127.0.0.1", timeout_s=1.0)
        assert _wait_until(lambda: receiver.stats()["rx_frames"] == len(frames))
        stats = receiver.stats()
        actual = receiver.drain(10)
    finally:
        receiver.stop()
        server.join(timeout=1.0)

    assert [frame.sequence for frame in actual] == [7, 8, 9]
    assert stats["dropped_frames"] == 7


@pytest.mark.skipif(
    not native_tcp_transport_available(), reason="尚未构建 C++ TCP 接收器")
def test_Cpp接收线程跳过坏帧后继续恢复():
    good = encode_v2_frame(V2Frame(MessageType.HEARTBEAT, sequence=22))
    broken = bytearray(good)
    broken[-3] ^= 0x80
    port, server = _start_server([bytes(broken) + good])
    receiver = NativeTcpV2Receiver()
    try:
        receiver.start(
            "127.0.0.1", port, local_host="127.0.0.1", timeout_s=1.0)
        assert _wait_until(lambda: receiver.stats()["rx_frames"] == 1)
        stats = receiver.stats()
        actual = receiver.drain()
    finally:
        receiver.stop()
        server.join(timeout=1.0)

    assert [frame.sequence for frame in actual] == [22]
    assert stats["decoder_errors"] == 1
