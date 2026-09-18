#pragma once

#include "motor_core/online_rls.hpp"

#include <atomic>
#include <array>
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

struct F2Sample {
    std::uint32_t tick_ms = 0;
    std::uint16_t adc1_raw = 0;
    std::uint16_t adc2_raw = 0;
    std::uint16_t offset_a = 0;
    std::uint16_t offset_b = 0;
    std::uint8_t sector = 0;
    std::uint16_t duty_a = 0;
    std::uint16_t duty_b = 0;
    std::uint16_t duty_c = 0;
    std::uint16_t sample_point = 0;
    double adc1_v = 0.0;
    double adc2_v = 0.0;
    double zero_a_v = 0.0;
    double zero_b_v = 0.0;
    double adc1_delta_a = 0.0;
    double adc2_delta_a = 0.0;
    bool has_calibration = false;
    std::uint16_t cal_adc1_min = 0;
    std::uint16_t cal_adc1_max = 0;
    std::uint16_t cal_adc2_min = 0;
    std::uint16_t cal_adc2_max = 0;
    bool has_vdda = false;
    double vdda_v = 0.0;
};

struct F3Sample {
    std::uint32_t tick_ms = 0;
    std::uint32_t updates = 0;
    double innov_rms_a = 0.0;
    double innov_rms_digit = 0.0;
    double p_trace = 0.0;
    std::array<float, 7> theta_d{};
    std::array<float, 7> theta_q{};
    double a1_d = 0.0;
    double a1_q = 0.0;
    double b_dd0_si = 0.0;
    double b_qq0_si = 0.0;
    double ld_mh = 0.0;
    double lq_mh = 0.0;
    double rd_ohm = 0.0;
    double rq_ohm = 0.0;
};

struct TelemetryProcessorStats {
    std::uint64_t f1_frames = 0;
    std::uint64_t f2_frames = 0;
    std::uint64_t f3_frames = 0;
    std::uint64_t f4_frames = 0;
    std::uint64_t f1_samples = 0;
    std::uint64_t completed_bursts = 0;
    std::uint64_t parse_errors = 0;
    std::uint64_t dropped_samples = 0;
    std::uint64_t dropped_bursts = 0;
    std::uint64_t dropped_diagnostics = 0;
    std::size_t queued_samples = 0;
    std::size_t queued_f2 = 0;
    std::size_t queued_f3 = 0;
    std::size_t queued_bursts = 0;
};

class TelemetryProcessor {
public:
    explicit TelemetryProcessor(std::size_t max_f1_samples = 131072,
                                std::size_t max_bursts = 4,
                                std::size_t max_diagnostics = 4096);

    bool ingest(std::uint8_t command, const std::uint8_t* payload,
                std::size_t size);
    void set_f1_rate_hz(std::uint32_t value) noexcept;
    void set_rls_coefficients_si(bool value) noexcept;
    void set_host_rls_enabled(bool enabled, bool reset = true);
    bool host_rls_enabled() const noexcept;
    std::vector<F1Sample> drain_f1(std::size_t max_samples = 8192);
    std::vector<F2Sample> drain_f2(std::size_t max_samples = 512);
    std::vector<F3Sample> drain_f3(std::size_t max_samples = 512);
    std::vector<BurstCapture> drain_bursts(std::size_t max_bursts = 1);
    TelemetryProcessorStats stats() const;
    void reset();
    void reset_burst();

private:
    bool ingest_f1(const std::uint8_t* payload, std::size_t size);
    bool ingest_f2(const std::uint8_t* payload, std::size_t size);
    bool ingest_f3(const std::uint8_t* payload, std::size_t size);
    bool ingest_f4(const std::uint8_t* payload, std::size_t size);
    void note_parse_error(std::uint8_t command);

    const std::size_t max_f1_samples_;
    const std::size_t max_bursts_;
    const std::size_t max_diagnostics_;
    std::atomic<std::uint32_t> f1_rate_hz_{1000};
    std::atomic<bool> rls_coefficients_si_{false};
    OnlineRlsEstimator host_rls_;
    mutable std::mutex mutex_;
    std::deque<F1Sample> f1_queue_;
    std::deque<F2Sample> f2_queue_;
    std::deque<F3Sample> f3_queue_;
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
