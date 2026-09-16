#include "motor_core/tcp_v2_receiver.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>

#ifdef _WIN32
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <WinSock2.h>
#include <WS2tcpip.h>
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/tcp.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace motor_core {
namespace {

#ifdef _WIN32
using NativeSocket = SOCKET;
constexpr NativeSocket kInvalidSocket = INVALID_SOCKET;

class SocketRuntime {
public:
    SocketRuntime() {
        WSADATA data{};
        if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
            throw std::runtime_error("WSAStartup failed");
        }
    }
    ~SocketRuntime() { WSACleanup(); }
};

void ensure_socket_runtime() {
    static SocketRuntime runtime;
    (void)runtime;
}

int last_socket_error() noexcept { return WSAGetLastError(); }
bool connect_in_progress(int error) noexcept {
    return error == WSAEWOULDBLOCK || error == WSAEINPROGRESS;
}
bool interrupted(int error) noexcept { return error == WSAEINTR; }
void close_socket(NativeSocket socket) noexcept { closesocket(socket); }
void shutdown_socket(NativeSocket socket) noexcept { shutdown(socket, SD_BOTH); }
bool set_nonblocking(NativeSocket socket, bool enabled) noexcept {
    u_long mode = enabled ? 1UL : 0UL;
    return ioctlsocket(socket, FIONBIO, &mode) == 0;
}
#else
using NativeSocket = int;
constexpr NativeSocket kInvalidSocket = -1;

void ensure_socket_runtime() {}
int last_socket_error() noexcept { return errno; }
bool connect_in_progress(int error) noexcept { return error == EINPROGRESS; }
bool interrupted(int error) noexcept { return error == EINTR; }
void close_socket(NativeSocket socket) noexcept { ::close(socket); }
void shutdown_socket(NativeSocket socket) noexcept { shutdown(socket, SHUT_RDWR); }
bool set_nonblocking(NativeSocket socket, bool enabled) noexcept {
    const int flags = fcntl(socket, F_GETFL, 0);
    if (flags < 0) {
        return false;
    }
    const int updated = enabled ? (flags | O_NONBLOCK) : (flags & ~O_NONBLOCK);
    return fcntl(socket, F_SETFL, updated) == 0;
}
#endif

constexpr std::uintptr_t kInvalidSocketValue =
    std::numeric_limits<std::uintptr_t>::max();

std::uintptr_t to_value(NativeSocket socket) noexcept {
    return static_cast<std::uintptr_t>(socket);
}

NativeSocket from_value(std::uintptr_t value) noexcept {
    return static_cast<NativeSocket>(value);
}

std::string socket_error_message(const char* operation, int error) {
    return std::string(operation) + " failed (socket error " +
           std::to_string(error) + ")";
}

bool bind_local_address(NativeSocket socket, int family,
                        const std::string& local_host) {
    if (local_host.empty()) {
        return true;
    }
    addrinfo hints{};
    hints.ai_family = family;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    addrinfo* addresses = nullptr;
    if (getaddrinfo(local_host.c_str(), "0", &hints, &addresses) != 0) {
        return false;
    }
    bool bound = false;
    for (auto* current = addresses; current != nullptr; current = current->ai_next) {
        if (::bind(socket, current->ai_addr,
                   static_cast<int>(current->ai_addrlen)) == 0) {
            bound = true;
            break;
        }
    }
    freeaddrinfo(addresses);
    return bound;
}

bool wait_until_connected(NativeSocket socket, double timeout_s) {
    fd_set write_set;
    fd_set error_set;
    FD_ZERO(&write_set);
    FD_ZERO(&error_set);
    FD_SET(socket, &write_set);
    FD_SET(socket, &error_set);
    const double clamped = std::max(0.01, timeout_s);
    timeval timeout{};
    timeout.tv_sec = static_cast<long>(clamped);
    timeout.tv_usec = static_cast<long>((clamped - timeout.tv_sec) * 1'000'000.0);
#ifdef _WIN32
    const int selected = select(0, nullptr, &write_set, &error_set, &timeout);
#else
    const int selected = select(socket + 1, nullptr, &write_set, &error_set, &timeout);
#endif
    if (selected <= 0 || FD_ISSET(socket, &error_set)) {
        return false;
    }
    int socket_error = 0;
#ifdef _WIN32
    int size = sizeof(socket_error);
#else
    socklen_t size = sizeof(socket_error);
#endif
    return getsockopt(socket, SOL_SOCKET, SO_ERROR,
                      reinterpret_cast<char*>(&socket_error), &size) == 0 &&
           socket_error == 0;
}

NativeSocket connect_socket(const std::string& host, std::uint16_t port,
                            const std::string& local_host, double timeout_s) {
    ensure_socket_runtime();
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    addrinfo* addresses = nullptr;
    const std::string service = std::to_string(port);
    const int lookup = getaddrinfo(host.c_str(), service.c_str(), &hints, &addresses);
    if (lookup != 0) {
        throw std::runtime_error("TCP address lookup failed: " + host);
    }

    NativeSocket connected = kInvalidSocket;
    int final_error = 0;
    for (auto* current = addresses; current != nullptr; current = current->ai_next) {
        NativeSocket socket = ::socket(current->ai_family, current->ai_socktype,
                                       current->ai_protocol);
        if (socket == kInvalidSocket) {
            final_error = last_socket_error();
            continue;
        }
        int enabled = 1;
        setsockopt(socket, IPPROTO_TCP, TCP_NODELAY,
                   reinterpret_cast<const char*>(&enabled), sizeof(enabled));
        int receive_buffer = 4 * 1024 * 1024;
        setsockopt(socket, SOL_SOCKET, SO_RCVBUF,
                   reinterpret_cast<const char*>(&receive_buffer),
                   sizeof(receive_buffer));
        if (!bind_local_address(socket, current->ai_family, local_host) ||
            !set_nonblocking(socket, true)) {
            final_error = last_socket_error();
            close_socket(socket);
            continue;
        }

        const int result = ::connect(socket, current->ai_addr,
                                     static_cast<int>(current->ai_addrlen));
        if (result == 0 ||
            (connect_in_progress(last_socket_error()) &&
             wait_until_connected(socket, timeout_s))) {
            if (!set_nonblocking(socket, false)) {
                final_error = last_socket_error();
                close_socket(socket);
                continue;
            }
            connected = socket;
            break;
        }
        final_error = last_socket_error();
        close_socket(socket);
    }
    freeaddrinfo(addresses);
    if (connected == kInvalidSocket) {
        throw std::runtime_error(socket_error_message("TCP connect", final_error));
    }
    return connected;
}

}  // namespace

TcpV2Receiver::TcpV2Receiver(std::size_t max_queue_frames)
    : max_queue_frames_(std::max<std::size_t>(1, max_queue_frames)),
      socket_value_(kInvalidSocketValue) {}

TcpV2Receiver::~TcpV2Receiver() { stop(); }

void TcpV2Receiver::start(const std::string& host, std::uint16_t port,
                          const std::string& local_host, double timeout_s) {
    stop();
    decoder_.reset();
    {
        std::lock_guard<std::mutex> lock(queue_mutex_);
        queue_.clear();
        last_error_.clear();
    }
    rx_bytes_ = 0;
    rx_frames_ = 0;
    dropped_frames_ = 0;
    decoder_errors_ = 0;
    stop_requested_ = false;

    const NativeSocket socket = connect_socket(host, port, local_host, timeout_s);
    {
        std::lock_guard<std::mutex> lock(socket_mutex_);
        socket_value_ = to_value(socket);
    }
    running_ = true;
    try {
        worker_ = std::thread(&TcpV2Receiver::receive_loop, this, to_value(socket));
    } catch (...) {
        running_ = false;
        {
            std::lock_guard<std::mutex> lock(socket_mutex_);
            socket_value_ = kInvalidSocketValue;
            close_socket(socket);
        }
        throw;
    }
}

void TcpV2Receiver::stop() noexcept {
    stop_requested_ = true;
    {
        std::lock_guard<std::mutex> lock(socket_mutex_);
        const auto value = socket_value_.load();
        if (value != kInvalidSocketValue) {
            shutdown_socket(from_value(value));
        }
    }
    if (worker_.joinable()) {
        worker_.join();
    }
    running_ = false;
}

std::vector<Frame> TcpV2Receiver::drain(std::size_t max_frames) {
    std::vector<Frame> frames;
    if (max_frames == 0U) {
        return frames;
    }
    std::lock_guard<std::mutex> lock(queue_mutex_);
    const auto count = std::min(max_frames, queue_.size());
    frames.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
        frames.push_back(std::move(queue_.front()));
        queue_.pop_front();
    }
    return frames;
}

ReceiverStats TcpV2Receiver::stats() const {
    ReceiverStats result;
    result.running = running_.load();
    result.rx_bytes = rx_bytes_.load();
    result.rx_frames = rx_frames_.load();
    result.dropped_frames = dropped_frames_.load();
    result.decoder_errors = decoder_errors_.load();
    std::lock_guard<std::mutex> lock(queue_mutex_);
    result.queued_frames = queue_.size();
    result.last_error = last_error_;
    return result;
}

void TcpV2Receiver::receive_loop(std::uintptr_t socket_value) noexcept {
    const NativeSocket socket = from_value(socket_value);
    std::array<std::uint8_t, 64 * 1024> bytes{};
    while (!stop_requested_.load()) {
        const int received = ::recv(
            socket, reinterpret_cast<char*>(bytes.data()),
            static_cast<int>(bytes.size()), 0);
        if (received > 0) {
            rx_bytes_ += static_cast<std::uint64_t>(received);
            auto frames = decoder_.feed(bytes.data(), static_cast<std::size_t>(received));
            decoder_errors_ = decoder_.error_count();
            rx_frames_ += static_cast<std::uint64_t>(frames.size());
            if (!frames.empty()) {
                std::lock_guard<std::mutex> lock(queue_mutex_);
                for (auto& frame : frames) {
                    if (queue_.size() >= max_queue_frames_) {
                        queue_.pop_front();
                        ++dropped_frames_;
                    }
                    queue_.push_back(std::move(frame));
                }
            }
            continue;
        }
        if (received == 0) {
            if (!stop_requested_.load()) {
                set_last_error("TCP peer closed the telemetry connection");
            }
            break;
        }
        const int error = last_socket_error();
        if (interrupted(error)) {
            continue;
        }
        if (!stop_requested_.load()) {
            set_last_error(socket_error_message("TCP recv", error));
        }
        break;
    }
    running_ = false;
    {
        std::lock_guard<std::mutex> lock(socket_mutex_);
        close_socket(socket);
        socket_value_ = kInvalidSocketValue;
    }
}

void TcpV2Receiver::set_last_error(std::string message) {
    std::lock_guard<std::mutex> lock(queue_mutex_);
    last_error_ = std::move(message);
}

}  // namespace motor_core
