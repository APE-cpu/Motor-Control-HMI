#include "motor_core/telemetry_processor.hpp"

#include <algorithm>
#include <utility>

namespace motor_core {
namespace {

constexpr std::uint8_t kF1Command = 0xF1;
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

}  // namespace

TelemetryProcessor::TelemetryProcessor(std::size_t max_f1_samples,
                                       std::size_t max_bursts)
    : max_f1_samples_(std::max<std::size_t>(1, max_f1_samples)),
      max_bursts_(std::max<std::size_t>(1, max_bursts)) {}

bool TelemetryProcessor::ingest(std::uint8_t command,
                                const std::uint8_t* payload,
                                std::size_t size) {
    if (command == kF1Command) {
        return ingest_f1(payload, size);
    }
    if (command == kF4Command) {
        return ingest_f4(payload, size);
    }
    return false;
}

void TelemetryProcessor::set_f1_rate_hz(std::uint32_t value) noexcept {
    f1_rate_hz_ = std::max<std::uint32_t>(1, value);
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
    } else if (command == kF4Command) {
        ++counters_.f4_frames;
    }
    ++counters_.parse_errors;
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
    result.queued_bursts = burst_queue_.size();
    return result;
}

void TelemetryProcessor::reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    f1_queue_.clear();
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
