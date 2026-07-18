import socket
import threading

from communications.tcp_comm import TCPComm


def test_tcp_comm_acts_as_client_and_exchanges_bytes():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        with conn:
            assert conn.recv(4) == b"ping"
            conn.sendall(b"pong")
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    comm = TCPComm()
    try:
        assert comm.open(host="127.0.0.1", port=port, timeout=1.0)
        assert comm.send(b"ping") == 4
        assert comm.recv(4, timeout=1.0) == b"pong"
    finally:
        comm.close()
        thread.join(timeout=2.0)
    assert not thread.is_alive()
