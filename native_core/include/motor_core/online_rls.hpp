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
    double id_a = 0.0;
    bool has_direct_id = false;
    double ia_a = 0.0;
    double ib_a = 0.0;
    double vd_raw = 0.0;
    double vq_raw = 0.0;
    double vbus_v = 0.0;
    double vd_applied_v = 0.0;
    double vq_applied_v = 0.0;
    bool has_applied_voltage = false;
};

struct RlsResult {
    std::uint32_t tick_ms = 0;
    std::uint64_t updates = 0;
    double innov_rms_a = 0.0;
    double p_trace = 0.0;
    double id_hat_a = 0.0;
    double iq_hat_a = 0.0;
    double id_prediction_a = 0.0;
    double iq_prediction_a = 0.0;
    double innovation_d = 0.0;
    double innovation_q = 0.0;
    std::array<double, 7> theta_d{};
    std::array<double, 7> theta_q{};
};

struct RlsDqInput {
    std::uint32_t tick_ms = 0;
    std::uint32_t rate_hz = 100000;
    double id_a = 0.0;
    double iq_a = 0.0;
    double ud_v = 0.0;
    double uq_v = 0.0;
};

// Host-side ESO + ARX(3, 2-input) RLS. Live hardware uses the capture rate,
// configured nominal inductance and reconstructed PWM-average dq voltage.
// An explicit reference flag retains exact R2024b Simulink replay for tests.
// All estimator work stays outside the firmware/current ISR.
class OnlineRlsEstimator {
public:
    explicit OnlineRlsEstimator(double nominal_inductance_h = 0.00066,
                                bool exact_simulink_reference = false,
                                double bandwidth_rad_s = 4000.0,
                                double forgetting_factor = 1.0,
                                bool rls_enabled = true);
    void set_rls_adaptation(bool enabled);
    void set_enabled(bool enabled, bool reset = true);
    bool enabled() const noexcept;
    void reset();

    // Returns true at the 10 Hz reporting cadence and writes a coherent result.
    bool ingest(const RlsInput& input, RlsResult& result);
    // Direct dq entry used by offline hardware analysis and reference replay.
    // Setting
    // force_snapshot returns the coefficient vector after this sample even
    // when the normal 10 Hz report cadence has not elapsed.
    bool ingest_dq(const RlsDqInput& input, RlsResult& result,
                   bool force_snapshot = false);

private:
    static constexpr std::size_t kTheta = 7;
    static constexpr std::size_t kAr = 3;

    struct Axis {
        std::array<double, kTheta> theta{};
        std::array<std::array<double, kTheta>, kTheta> covariance{};
    };

    struct EsoAxis {
        double state_current = 0.0;
        double state_disturbance = 0.0;
        double delayed_voltage = 0.0;
        double input_gain = 0.0005165289256;
        double current_gain = 0.07725282631245653;
        double disturbance_gain = 3.0057064158538105;

        void configure(std::uint32_t rate_hz, double nominal_inductance_h,
                       bool exact_simulink_reference, double bandwidth_rad_s) noexcept;
        void reset() noexcept;
        double step(double measured_current, double applied_voltage) noexcept;
    };

    void reset_unlocked(std::uint32_t rate_hz);
    bool update_axis(Axis& axis,
                            const std::array<double, kTheta>& regressor,
                            double output, double& innovation);
    bool ingest_dq_unlocked(const RlsDqInput& input, RlsResult& result,
                            bool force_snapshot);
    bool snapshot_unlocked(std::uint32_t tick_ms, RlsResult& result) const;

    mutable std::mutex mutex_;
    double nominal_inductance_h_ = 0.00066;
    bool exact_simulink_reference_ = false;
    double bandwidth_rad_s_ = 4000.0;
    double forgetting_factor_ = 1.0;
    bool rls_enabled_ = true;
    bool rls_adaptation_ = true;
    double id_prediction_a_ = 0.0;
    double iq_prediction_a_ = 0.0;
    double innovation_d_ = 0.0;
    double innovation_q_ = 0.0;
    bool enabled_ = false;
    std::uint32_t rate_hz_ = 16000;
    Axis d_;
    Axis q_;
    EsoAxis eso_d_;
    EsoAxis eso_q_;
    double id_hat_a_ = 0.0;
    double iq_hat_a_ = 0.0;
    std::array<double, kAr> id_history_{};
    std::array<double, kAr> iq_history_{};
    std::array<double, kAr> vd_history_{};
    std::array<double, kAr> vq_history_{};
    std::uint32_t primed_ = 0;
    std::uint64_t updates_ = 0;
    std::uint32_t samples_since_report_ = 0;
    double innovation_power_ema_ = 0.0;
};

}  // namespace motor_core
