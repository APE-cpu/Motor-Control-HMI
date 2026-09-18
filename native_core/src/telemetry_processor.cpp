#include "motor_core/telemetry_processor.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <utility>

namespace motor_core {
namespace {

struct EquivalentRl {
    double resistance = std::numeric_limits<double>::quiet_NaN();
    double inductance = std::numeric_limits<double>::quiet_NaN();
};

EquivalentRl arx3_first_order_equivalent(
        const std::array<float, 7>& theta,
        std::size_t own_voltage_index,
        double coefficient_scale,
        double sample_time) {
    const double a1 = theta[0];
    const double a2 = theta[1];
    const double a3 = theta[2];
    const double b0 = theta[own_voltage_index] * coefficient_scale;
    const double b1 = theta[own_voltage_index + 1U] * coefficient_scale;
    const double a_dc = 1.0 - a1 - a2 - a3;
    const double b_dc = b0 + b1;
    constexpr double kEpsilon = 1e-12;
    if (!std::isfinite(a_dc) || !std::isfinite(b_dc) ||
            std::abs(a_dc) <= kEpsilon || std::abs(b_dc) <= kEpsilon) {
        return {};
    }

    const double resistance = a_dc / b_dc;
    // H(q)=(b0*q+b1*q^2)/(1-a1*q-a2*q^2-a3*q^3), q=z^-1.
    // The normalized first moment at q=1 preserves the original first-order
    // L=Ts/b0 and R=(1-a1)/b0 result, while using every ARX coefficient.
    const double moment = (b0 + 2.0 * b1) / b_dc +
        (a1 + 2.0 * a2 + 3.0 * a3) / a_dc;
    const double inductance = resistance * sample_time * moment;
    if (!(resistance > 0.0) || !(inductance > 0.0) ||
            !std::isfinite(inductance)) {
        return {};
    }
    return {resistance, inductance};
}

constexpr std::uint8_t kF1Command = 0xF1;
constexpr std::uint8_t kF2Command = 0xF2;
constexpr std::uint8_t kF3Command = 0xF3;
constexpr std::uint8_t kF4Command = 0xF4;
constexpr double kCurrentScale = 0.000629;
constexpr double kAngleScale = 360.0 / 65536.0;

std::uint16_t read_u16_le(const std::uint8_t* data) noexcept {
    return static_cast<std::uint16_t>(data[0]) |
           (static_cast<std::uint16_t>(data[1]) << 8U);
}

std::int16_t read_i16_le(const std::uint8_t* data) noexcept {
    return static_cast<std::int16_t>(read_u16_le(data));
}

std::uint32_t read_u32_le(const std::uint8_t* data) noexcept {
    return static_cast<std::uint32_t>(data[0]) |
           (static_cast<std::uint32_t>(data[1]) << 8U) |
           (static_cast<std::uint32_t>(data[2]) << 16U) |
           (static_cast<std::uint32_t>(data[3]) << 24U);
}

float read_f32_le(const std::uint8_t* data) noexcept {
    const auto bits = read_u32_le(data);
    float value = 0.0F;
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

}  // namespace

TelemetryProcessor::TelemetryProcessor(std::size_t max_f1_samples,
                                       std::size_t max_bursts,
                                       std::size_t max_diagnostics)
    : max_f1_samples_(std::max<std::size_t>(1, max_f1_samples)),
      max_bursts_(std::max<std::size_t>(1, max_bursts)),
      max_diagnostics_(std::max<std::size_t>(1, max_diagnostics)) {}

bool TelemetryProcessor::ingest(std::uint8_t command,
                                const std::uint8_t* payload,
                                std::size_t size) {
    if (command == kF1Command) {
        return ingest_f1(payload, size);
    }
    if (command == kF2Command) {
        return ingest_f2(payload, size);
    }
    if (command == kF3Command) {
        return ingest_f3(payload, size);
    }
    if (command == kF4Command) {
        return ingest_f4(payload, size);
    }
    return false;
}

void TelemetryProcessor::set_f1_rate_hz(std::uint32_t value) noexcept {
    f1_rate_hz_ = std::max<std::uint32_t>(1, value);
}

void TelemetryProcessor::set_rls_coefficients_si(bool value) noexcept {
    rls_coefficients_si_ = value;
}

void TelemetryProcessor::set_host_rls_enabled(bool enabled, bool reset) {
    host_rls_.set_enabled(enabled, reset);
}

bool TelemetryProcessor::host_rls_enabled() const noexcept {
    return host_rls_.enabled();
}

bool TelemetryProcessor::ingest_f1(const std::uint8_t* payload,
                                   std::size_t size) {
    std::size_t sample_size = 0;
    if (size == 12U) {
        sample_size = 12U;
    } else if (size >= 22U && size % 22U == 0U) {
        sample_size = 22U;
    } else if (size >= 16U && size % 16U == 0U) {
        sample_size = 16U;
    }
    if (sample_size == 0U) {
        note_parse_error(kF1Command);
        return true;
    }

    std::vector<F1Sample> parsed;
    std::vector<F3Sample> host_rls_results;
    parsed.reserve(size / sample_size);
    const auto rate_hz = f1_rate_hz_.load();
    for (std::size_t offset = 0; offset < size; offset += sample_size) {
        const auto* sample = payload + offset;
        F1Sample result;
        result.tick_ms = read_u32_le(sample);
        result.rate_hz = rate_hz;
        result.angle_deg = read_u16_le(sample + 4U) * kAngleScale;
        result.speed_rpm = static_cast<double>(read_i16_le(sample + 6U));
        result.iq_a = read_i16_le(sample + 8U) * kCurrentScale;
        result.iqref_a = read_i16_le(sample + 10U) * kCurrentScale;
        if (sample_size >= 16U) {
            result.has_phase_current = true;
            result.ia_a = read_i16_le(sample + 12U) * kCurrentScale;
            result.ib_a = read_i16_le(sample + 14U) * kCurrentScale;
        }
        if (sample_size >= 22U) {
            result.has_voltage = true;
            result.vd_raw = static_cast<double>(read_i16_le(sample + 16U));
            result.vq_raw = static_cast<double>(read_i16_le(sample + 18U));
            result.vbus_v = static_cast<double>(read_u16_le(sample + 20U));
        }
        if (result.has_phase_current && result.has_voltage) {
            RlsResult estimate;
            const RlsInput input{
                result.tick_ms, result.rate_hz, result.angle_deg,
                result.iq_a, result.ia_a, result.ib_a,
                result.vd_raw, result.vq_raw, result.vbus_v};
            if (host_rls_.ingest(input, estimate)) {
                F3Sample local;
                local.tick_ms = estimate.tick_ms;
                local.updates = static_cast<std::uint32_t>(std::min<std::uint64_t>(
                    estimate.updates,
                    std::numeric_limits<std::uint32_t>::max()));
                local.innov_rms_a = estimate.innov_rms_a;
                local.innov_rms_digit =
                    std::numeric_limits<double>::quiet_NaN();
                local.p_trace = estimate.p_trace;
                for (std::size_t i = 0; i < local.theta_d.size(); ++i) {
                    local.theta_d[i] = static_cast<float>(estimate.theta_d[i]);
                    local.theta_q[i] = static_cast<float>(estimate.theta_q[i]);
                }
                local.a1_d = local.theta_d[0];
                local.a1_q = local.theta_q[0];
                local.b_dd0_si = local.theta_d[3];
                local.b_qq0_si = local.theta_q[5];
                const double sample_time = 1.0 /
                    static_cast<double>(std::max<std::uint32_t>(1U, rate_hz));
                const auto d_equivalent = arx3_first_order_equivalent(
                    local.theta_d, 3U, 1.0, sample_time);
                const auto q_equivalent = arx3_first_order_equivalent(
                    local.theta_q, 5U, 1.0, sample_time);
                local.ld_mh = d_equivalent.inductance * 1e3;
                local.lq_mh = q_equivalent.inductance * 1e3;
                local.rd_ohm = d_equivalent.resistance;
                local.rq_ohm = q_equivalent.resistance;
                host_rls_results.push_back(std::move(local));
            }
        }
        parsed.push_back(result);
    }

    std::lock_guard<std::mutex> lock(mutex_);
    ++counters_.f1_frames;
    counters_.f1_samples += parsed.size();
    for (auto& sample : parsed) {
        if (f1_queue_.size() >= max_f1_samples_) {
            f1_queue_.pop_front();
            ++counters_.dropped_samples;
        }
        f1_queue_.push_back(std::move(sample));
    }
    for (auto& sample : host_rls_results) {
        if (f3_queue_.size() >= max_diagnostics_) {
            f3_queue_.pop_front();
            ++counters_.dropped_diagnostics;
        }
        f3_queue_.push_back(std::move(sample));
        ++counters_.f3_frames;
    }
    return true;
}

bool TelemetryProcessor::ingest_f2(const std::uint8_t* payload,
                                   std::size_t size) {
    if (size != 21U && size != 29U && size != 31U) {
        note_parse_error(kF2Command);
        return true;
    }
    F2Sample result;
    result.tick_ms = read_u32_le(payload);
    result.adc1_raw = read_u16_le(payload + 4U);
    result.adc2_raw = read_u16_le(payload + 6U);
    result.offset_a = read_u16_le(payload + 8U);
    result.offset_b = read_u16_le(payload + 10U);
    result.sector = payload[12U];
    result.duty_a = read_u16_le(payload + 13U);
    result.duty_b = read_u16_le(payload + 15U);
    result.duty_c = read_u16_le(payload + 17U);
    result.sample_point = read_u16_le(payload + 19U);
    constexpr double kAdcVoltsPerCount = 3.3 / 32768.0;
    constexpr double kAmpsPerMcDigit = 3.3 / (65536.0 * 0.01 * 8.0);
    result.adc1_v = result.adc1_raw * kAdcVoltsPerCount;
    result.adc2_v = result.adc2_raw * kAdcVoltsPerCount;
    result.zero_a_v = (result.offset_a / 2.0) * kAdcVoltsPerCount;
    result.zero_b_v = (result.offset_b / 2.0) * kAdcVoltsPerCount;
    result.adc1_delta_a =
        (static_cast<int>(result.offset_a) - 2 * result.adc1_raw) *
        kAmpsPerMcDigit;
    result.adc2_delta_a =
        (static_cast<int>(result.offset_b) - 2 * result.adc2_raw) *
        kAmpsPerMcDigit;
    if (size >= 29U) {
        result.has_calibration = true;
        result.cal_adc1_min = read_u16_le(payload + 21U);
        result.cal_adc1_max = read_u16_le(payload + 23U);
        result.cal_adc2_min = read_u16_le(payload + 25U);
        result.cal_adc2_max = read_u16_le(payload + 27U);
    }
    if (size >= 31U) {
        const auto vdda_mv = read_u16_le(payload + 29U);
        if (vdda_mv != 0U) {
            result.has_vdda = true;
            result.vdda_v = vdda_mv / 1000.0;
        }
    }

    std::lock_guard<std::mutex> lock(mutex_);
    ++counters_.f2_frames;
    if (f2_queue_.size() >= max_diagnostics_) {
        f2_queue_.pop_front();
        ++counters_.dropped_diagnostics;
    }
    f2_queue_.push_back(std::move(result));
    return true;
}

bool TelemetryProcessor::ingest_f3(const std::uint8_t* payload,
                                   std::size_t size) {
    if (size != 72U) {
        note_parse_error(kF3Command);
        return true;
    }
    F3Sample result;
    result.tick_ms = read_u32_le(payload);
    result.updates = read_u32_le(payload + 4U);
    const double innov_rms = read_f32_le(payload + 8U);
    result.p_trace = read_f32_le(payload + 12U);
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const bool coefficients_are_si = rls_coefficients_si_.load();
    result.innov_rms_a = coefficients_are_si ? innov_rms : nan;
    result.innov_rms_digit = coefficients_are_si ? nan : innov_rms;
    for (std::size_t index = 0; index < 7U; ++index) {
        result.theta_d[index] = read_f32_le(payload + 16U + index * 4U);
        result.theta_q[index] = read_f32_le(payload + 44U + index * 4U);
    }
    result.a1_d = result.theta_d[0];
    result.a1_q = result.theta_q[0];
    double coefficient_scale = 1.0;
    if (!coefficients_are_si) {
        constexpr double kCurrentLsb =
            (3.30 / 2.0) / (0.01 * 8.0) / 32767.0;
        const double voltage_lsb = 24.0 / std::sqrt(3.0) / 32767.0;
        coefficient_scale = kCurrentLsb / voltage_lsb;
    }
    result.b_dd0_si = result.theta_d[3] * coefficient_scale;
    result.b_qq0_si = result.theta_q[5] * coefficient_scale;
    constexpr double kSampleTime = 1.0 / 16000.0;
    const auto d_equivalent = arx3_first_order_equivalent(
        result.theta_d, 3U, coefficient_scale, kSampleTime);
    const auto q_equivalent = arx3_first_order_equivalent(
        result.theta_q, 5U, coefficient_scale, kSampleTime);
    result.ld_mh = d_equivalent.inductance * 1e3;
    result.lq_mh = q_equivalent.inductance * 1e3;
    result.rd_ohm = d_equivalent.resistance;
    result.rq_ohm = q_equivalent.resistance;

    std::lock_guard<std::mutex> lock(mutex_);
    ++counters_.f3_frames;
    if (f3_queue_.size() >= max_diagnostics_) {
        f3_queue_.pop_front();
        ++counters_.dropped_diagnostics;
    }
    f3_queue_.push_back(std::move(result));
    return true;
}

bool TelemetryProcessor::ingest_f4(const std::uint8_t* payload,
                                   std::size_t size) {
    if (size < 6U) {
        note_parse_error(kF4Command);
        return true;
    }
    const auto total = read_u16_le(payload);
    const auto start = read_u16_le(payload + 2U);
    const auto count = read_u16_le(payload + 4U);
    const std::size_t expected = 6U + static_cast<std::size_t>(count) * 6U;
    if (total == 0U || count == 0U || expected > size ||
        static_cast<std::uint32_t>(start) + count > total) {
        note_parse_error(kF4Command);
        return true;
    }

    std::lock_guard<std::mutex> lock(mutex_);
    ++counters_.f4_frames;
    if (burst_total_ != total) {
        burst_total_ = total;
        burst_received_ = 0;
        burst_ia_.assign(total, 0);
        burst_ib_.assign(total, 0);
        burst_angle_.assign(total, 0);
        burst_present_.assign(total, 0);
    }
    for (std::size_t index = 0; index < count; ++index) {
        const std::size_t target = static_cast<std::size_t>(start) + index;
        const auto* sample = payload + 6U + index * 6U;
        burst_ia_[target] = read_i16_le(sample);
        burst_ib_[target] = read_i16_le(sample + 2U);
        burst_angle_[target] = read_u16_le(sample + 4U);
        if (burst_present_[target] == 0U) {
            burst_present_[target] = 1U;
            ++burst_received_;
        }
    }
    if (burst_received_ == burst_total_) {
        if (burst_queue_.size() >= max_bursts_) {
            burst_queue_.pop_front();
            ++counters_.dropped_bursts;
        }
        burst_queue_.push_back(BurstCapture{
            std::move(burst_ia_), std::move(burst_ib_),
            std::move(burst_angle_)});
        ++counters_.completed_bursts;
        burst_total_ = 0;
        burst_received_ = 0;
        burst_present_.clear();
    }
    return true;
}

void TelemetryProcessor::note_parse_error(std::uint8_t command) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (command == kF1Command) {
        ++counters_.f1_frames;
    } else if (command == kF2Command) {
        ++counters_.f2_frames;
    } else if (command == kF3Command) {
        ++counters_.f3_frames;
    } else if (command == kF4Command) {
        ++counters_.f4_frames;
    }
    ++counters_.parse_errors;
}

std::vector<F2Sample> TelemetryProcessor::drain_f2(std::size_t max_samples) {
    std::vector<F2Sample> samples;
    if (max_samples == 0U) {
        return samples;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    const auto count = std::min(max_samples, f2_queue_.size());
    samples.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
        samples.push_back(std::move(f2_queue_.front()));
        f2_queue_.pop_front();
    }
    return samples;
}

std::vector<F3Sample> TelemetryProcessor::drain_f3(std::size_t max_samples) {
    std::vector<F3Sample> samples;
    if (max_samples == 0U) {
        return samples;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    const auto count = std::min(max_samples, f3_queue_.size());
    samples.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
        samples.push_back(std::move(f3_queue_.front()));
        f3_queue_.pop_front();
    }
    return samples;
}

std::vector<F1Sample> TelemetryProcessor::drain_f1(std::size_t max_samples) {
    std::vector<F1Sample> samples;
    if (max_samples == 0U) {
        return samples;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    const auto count = std::min(max_samples, f1_queue_.size());
    samples.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
        samples.push_back(std::move(f1_queue_.front()));
        f1_queue_.pop_front();
    }
    return samples;
}

std::vector<BurstCapture> TelemetryProcessor::drain_bursts(
        std::size_t max_bursts) {
    std::vector<BurstCapture> captures;
    if (max_bursts == 0U) {
        return captures;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    const auto count = std::min(max_bursts, burst_queue_.size());
    captures.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
        captures.push_back(std::move(burst_queue_.front()));
        burst_queue_.pop_front();
    }
    return captures;
}

TelemetryProcessorStats TelemetryProcessor::stats() const {
    std::lock_guard<std::mutex> lock(mutex_);
    auto result = counters_;
    result.queued_samples = f1_queue_.size();
    result.queued_f2 = f2_queue_.size();
    result.queued_f3 = f3_queue_.size();
    result.queued_bursts = burst_queue_.size();
    return result;
}

void TelemetryProcessor::reset() {
    host_rls_.reset();
    std::lock_guard<std::mutex> lock(mutex_);
    f1_queue_.clear();
    f2_queue_.clear();
    f3_queue_.clear();
    burst_queue_.clear();
    burst_total_ = 0;
    burst_received_ = 0;
    burst_ia_.clear();
    burst_ib_.clear();
    burst_angle_.clear();
    burst_present_.clear();
    counters_ = {};
}

void TelemetryProcessor::reset_burst() {
    std::lock_guard<std::mutex> lock(mutex_);
    burst_total_ = 0;
    burst_received_ = 0;
    burst_ia_.clear();
    burst_ib_.clear();
    burst_angle_.clear();
    burst_present_.clear();
    burst_queue_.clear();
}

}  // namespace motor_core
