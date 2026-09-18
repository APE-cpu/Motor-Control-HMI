#pragma once

#include <array>
#include <cstdint>
#include <mutex>

namespace motor_core {

struct RlsInput {
    std::uint32_t tick_ms = 0;
    std::uint32_t rate_hz = 16000;
    double angle_deg = 0.0;
    double iq_a = 0.0;
    double ia_a = 0.0;
    double ib_a = 0.0;
    double vd_raw = 0.0;
    double vq_raw = 0.0;
    double vbus_v = 0.0;
};

struct RlsResult {
    std::uint32_t tick_ms = 0;
    std::uint64_t updates = 0;
    double innov_rms_a = 0.0;
    double p_trace = 0.0;
    std::array<double, 7> theta_d{};
    std::array<double, 7> theta_q{};
};

// Host-side online ARX(3, 2-input) RLS. The estimator consumes F1 samples in
// SI units and is deliberately isolated from the motor-control firmware/ISR.
class OnlineRlsEstimator {
public:
    void set_enabled(bool enabled, bool reset = true);
    bool enabled() const noexcept;
    void reset();

    // Returns true at the 10 Hz reporting cadence and writes a coherent result.
    bool ingest(const RlsInput& input, RlsResult& result);

private:
    static constexpr std::size_t kTheta = 7;
    static constexpr std::size_t kAr = 3;

    struct Axis {
        std::array<double, kTheta> theta{};
        std::array<std::array<double, kTheta>, kTheta> covariance{};
    };

    struct EsoAxis {
        double x_hat_a = 0.0;
        double f_hat_a_s = 0.0;
        double previous_voltage_v = 0.0;
        bool primed = false;
    };

    void reset_unlocked(std::uint32_t rate_hz);
    static bool update_axis(Axis& axis,
                            const std::array<double, kTheta>& regressor,
                            double output, double& innovation);
    double update_eso(EsoAxis& eso, double measured_a,
                      double voltage_v) const;
    bool snapshot_unlocked(std::uint32_t tick_ms, RlsResult& result) const;

    mutable std::mutex mutex_;
    bool enabled_ = false;
    std::uint32_t rate_hz_ = 16000;
    Axis d_;
    Axis q_;
    EsoAxis eso_d_;
    EsoAxis eso_q_;
    std::array<double, kAr> id_history_{};
    std::array<double, kAr> iq_history_{};
    std::array<double, 2> vd_history_{};
    std::array<double, 2> vq_history_{};
    std::uint32_t primed_ = 0;
    std::uint64_t updates_ = 0;
    std::uint32_t samples_since_report_ = 0;
    double innovation_power_ema_ = 0.0;
};

}  // namespace motor_core
