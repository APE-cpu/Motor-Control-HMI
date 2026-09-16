import socket
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

        def stats(self):
            return {
                "running": False, "rx_bytes": 0, "rx_frames": 5,
                "dropped_frames": 0, "decoder_errors": 0,
                "queued_frames": len(self.frames),
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
        assert _wait_until(lambda: receiver.stats()["rx_frames"] == len(frames))
        actual = receiver.drain(100)
        stats = receiver.stats()
    finally:
        receiver.stop()
        server.join(timeout=1.0)

    assert actual == frames
    assert stats["decoder_errors"] == 0
    assert stats["dropped_frames"] == 0


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
