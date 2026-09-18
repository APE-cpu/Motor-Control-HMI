#pragma once

#include "motor_core/protocol_v2.hpp"
#include "motor_core/telemetry_processor.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace motor_core {

struct ReceiverStats {
    bool running = false;
    std::uint64_t rx_bytes = 0;
    std::uint64_t rx_frames = 0;
    std::uint64_t dropped_frames = 0;
    std::uint64_t decoder_errors = 0;
    std::size_t queued_frames = 0;
    TelemetryProcessorStats telemetry;
    std::string last_error;
};

class TcpV2Receiver {
public:
    explicit TcpV2Receiver(std::size_t max_queue_frames = 8192,
                           std::size_t max_f1_samples = 131072);
    ~TcpV2Receiver();

    TcpV2Receiver(const TcpV2Receiver&) = delete;
    TcpV2Receiver& operator=(const TcpV2Receiver&) = delete;

    void start(const std::string& host, std::uint16_t port,
               const std::string& local_host = {}, double timeout_s = 2.0);
    void stop() noexcept;
    std::vector<Frame> drain(std::size_t max_frames = 512);
    std::vector<F1Sample> drain_f1(std::size_t max_samples = 8192);
    std::vector<F2Sample> drain_f2(std::size_t max_samples = 512);
    std::vector<F3Sample> drain_f3(std::size_t max_samples = 512);
    std::vector<BurstCapture> drain_bursts(std::size_t max_bursts = 1);
    void set_f1_rate_hz(std::uint32_t value) noexcept;
    void set_rls_coefficients_si(bool value) noexcept;
    void set_host_rls_enabled(bool enabled, bool reset = true);
    bool host_rls_enabled() const noexcept;
    void set_telemetry_processing_enabled(bool enabled) noexcept;
    void reset_burst();
    ReceiverStats stats() const;

private:
    void receive_loop(std::uintptr_t socket_value) noexcept;
    void set_last_error(std::string message);

    const std::size_t max_queue_frames_;
    mutable std::mutex socket_mutex_;
    mutable std::mutex queue_mutex_;
    std::deque<Frame> queue_;
    std::string last_error_;
    StreamDecoder decoder_;
    TelemetryProcessor telemetry_;
    std::thread worker_;
    std::atomic<bool> running_{false};
    std::atomic<bool> stop_requested_{false};
    std::atomic<bool> telemetry_processing_enabled_{true};
    std::atomic<std::uintptr_t> socket_value_;
    std::atomic<std::uint64_t> rx_bytes_{0};
    std::atomic<std::uint64_t> rx_frames_{0};
    std::atomic<std::uint64_t> dropped_frames_{0};
    std::atomic<std::uint64_t> decoder_errors_{0};
};

}  // namespace motor_core
