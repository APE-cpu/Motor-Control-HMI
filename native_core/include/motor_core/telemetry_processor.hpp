#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <vector>

namespace motor_core {

struct F1Sample {
    std::uint32_t tick_ms = 0;
    std::uint32_t rate_hz = 1000;
    double angle_deg = 0.0;
    double speed_rpm = 0.0;
    double iq_a = 0.0;
    double iqref_a = 0.0;
    double ia_a = 0.0;
    double ib_a = 0.0;
    double vd_raw = 0.0;
    double vq_raw = 0.0;
    double vbus_v = 0.0;
    bool has_phase_current = false;
    bool has_voltage = false;
};

struct BurstCapture {
    std::vector<std::int16_t> ia;
    std::vector<std::int16_t> ib;
    std::vector<std::uint16_t> angle;
};

struct TelemetryProcessorStats {
    std::uint64_t f1_frames = 0;
    std::uint64_t f4_frames = 0;
    std::uint64_t f1_samples = 0;
    std::uint64_t completed_bursts = 0;
    std::uint64_t parse_errors = 0;
    std::uint64_t dropped_samples = 0;
    std::uint64_t dropped_bursts = 0;
    std::size_t queued_samples = 0;
    std::size_t queued_bursts = 0;
};

class TelemetryProcessor {
public:
    explicit TelemetryProcessor(std::size_t max_f1_samples = 131072,
                                std::size_t max_bursts = 4);

    bool ingest(std::uint8_t command, const std::uint8_t* payload,
                std::size_t size);
    void set_f1_rate_hz(std::uint32_t value) noexcept;
    std::vector<F1Sample> drain_f1(std::size_t max_samples = 8192);
    std::vector<BurstCapture> drain_bursts(std::size_t max_bursts = 1);
    TelemetryProcessorStats stats() const;
    void reset();
    void reset_burst();

private:
    bool ingest_f1(const std::uint8_t* payload, std::size_t size);
    bool ingest_f4(const std::uint8_t* payload, std::size_t size);
    void note_parse_error(std::uint8_t command);

    const std::size_t max_f1_samples_;
    const std::size_t max_bursts_;
    std::atomic<std::uint32_t> f1_rate_hz_{1000};
    mutable std::mutex mutex_;
    std::deque<F1Sample> f1_queue_;
    std::deque<BurstCapture> burst_queue_;
    std::uint16_t burst_total_ = 0;
    std::size_t burst_received_ = 0;
    std::vector<std::int16_t> burst_ia_;
    std::vector<std::int16_t> burst_ib_;
    std::vector<std::uint16_t> burst_angle_;
    std::vector<std::uint8_t> burst_present_;
    TelemetryProcessorStats counters_;
};

}  // namespace motor_core
