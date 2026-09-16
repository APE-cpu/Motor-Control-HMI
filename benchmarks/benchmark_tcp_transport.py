"""本机 TCP 回环下比较旧 Python 路径与 C++ 接收线程。"""
from __future__ import annotations

import argparse
import socket
import threading
import time

from communications.native_transport import NativeTcpV2Receiver
from communications.protocol_v2 import (
    MessageType, V2Frame, V2StreamDecoder, encode_v2_frame,
)
from communications.tcp_comm import TCPComm


def make_wire(frame_count: int, payload_size: int) -> bytes:
    payload = bytes(index & 0xFF for index in range(payload_size))
    return b"".join(
        encode_v2_frame(V2Frame(
            MessageType.TELEMETRY, 0xF1, payload,
            sequence=index & 0xFFFF,
        ))
        for index in range(frame_count)
    )


def start_server(wire: bytes) -> tuple[int, threading.Thread]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                connection.sendall(wire)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread


def measure_python(wire: bytes, frame_count: int) -> float:
    port, server = start_server(wire)
    connection = TCPComm()
    decoder = V2StreamDecoder()
    decoded = 0
    started = time.perf_counter()
    connection.open("127.0.0.1", port, timeout=2.0, local_host="127.0.0.1")
    try:
        while decoded < frame_count:
            try:
                chunk = connection.recv(64 * 1024, timeout=1.0)
            except ConnectionError:
                break
            decoded += len(decoder.feed(chunk))
    finally:
        connection.close()
        server.join(timeout=2.0)
    assert decoded == frame_count
    return time.perf_counter() - started


def measure_native(wire: bytes, frame_count: int) -> float:
    port, server = start_server(wire)
    receiver = NativeTcpV2Receiver(max_queue_frames=frame_count + 1)
    # 这个基准只比较 TCP + 帧解码。F1/F4 处理由独立基准覆盖。
    receiver.set_telemetry_processing_enabled(False)
    started = time.perf_counter()
    receiver.start(
        "127.0.0.1", port, local_host="127.0.0.1", timeout_s=2.0)
    deadline = time.monotonic() + 10.0
    try:
        while receiver.stats()["rx_frames"] < frame_count:
            if time.monotonic() >= deadline:
                raise TimeoutError("C++ receiver benchmark timed out")
            time.sleep(0.001)
        decoded = len(receiver.drain(frame_count + 1))
    finally:
        receiver.stop()
        server.join(timeout=2.0)
    assert decoded == frame_count
    return time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=50_000)
    parser.add_argument("--payload", type=int, default=22)
    args = parser.parse_args()
    wire = make_wire(args.frames, args.payload)
    python_s = measure_python(wire, args.frames)
    native_s = measure_native(wire, args.frames)
    mib = len(wire) / (1024 * 1024)
    print(f"python: {mib / python_s:.2f} MiB/s, {args.frames / python_s:,.0f} frames/s")
    print(f"native: {mib / native_s:.2f} MiB/s, {args.frames / native_s:,.0f} frames/s")
    print(f"speedup: {python_s / native_s:.2f}x")


if __name__ == "__main__":
    main()
