#include "motor_core/online_rls.hpp"

#include <algorithm>
#include <cmath>

namespace motor_core {
namespace {

// Intentionally identical to the two RLS blocks in the R2024b model.
constexpr double kP0 = 1.0e6;
// Exact scalar parameters emitted by R2024b for both ESO axes in
// MFPCC_DDM_coldstart_ESOrls.slx (Ts=10 us, wo=4000 rad/s, delta=0.01).
// The source model motor is R=2.34 ohm, Ld=Lq=19.36 mH and flux=0.402 Wb.
// This observer exists solely for bit-compatible simulation replay.  It must
// not be interpreted as the physical observer for the F407 motor
// (0.59 ohm/0.66 mH/about 0.00585 Wb).
constexpr double kEsoInputGain = 0.0005165289256;
constexpr double kEsoCurrentGain = 0.07725282631245653;
constexpr double kEsoDisturbanceGain = 3.0057064158538105;
constexpr double kEsoModelRateHz = 100000.0;
constexpr double kSimulinkInductanceH = 19.36e-3;
constexpr double kSqrt3 = 1.7320508075688772935;
constexpr double kPi = 3.14159265358979323846;

bool finite_f1_input(const RlsInput& input) {
    return input.rate_hz > 0U && input.vbus_v > 1.0 &&
        std::isfinite(input.angle_deg) && std::isfinite(input.iq_a) &&
        (!input.has_direct_id || std::isfinite(input.id_a)) &&
        std::isfinite(input.ia_a) && std::isfinite(input.ib_a) &&
        std::isfinite(input.vd_raw) && std::isfinite(input.vq_raw) &&
        std::isfinite(input.vbus_v);
}

bool finite_dq_input(const RlsDqInput& input) {
    return input.rate_hz > 0U && std::isfinite(input.id_a) &&
        std::isfinite(input.iq_a) && std::isfinite(input.ud_v) &&
        std::isfinite(input.uq_v);
}

}  // namespace

OnlineRlsEstimator::OnlineRlsEstimator(
        double nominal_inductance_h,
        bool exact_simulink_reference, double bandwidth_rad_s,
        double forgetting_factor, bool rls_enabled)
    : nominal_inductance_h_(
          std::isfinite(nominal_inductance_h) && nominal_inductance_h > 0.0
              ? nominal_inductance_h : 0.00066),
      exact_simulink_reference_(exact_simulink_reference),
      bandwidth_rad_s_(bandwidth_rad_s),
      forgetting_factor_(forgetting_factor), rls_enabled_(rls_enabled) {}

void OnlineRlsEstimator::set_rls_adaptation(bool enabled) {
    std::lock_guard<std::mutex> lock(mutex_);
    rls_adaptation_ = enabled;
}

void OnlineRlsEstimator::EsoAxis::configure(
        std::uint32_t rate_hz, double nominal_inductance_h,
        bool exact_simulink_reference, double bandwidth_rad_s) noexcept {
    if (exact_simulink_reference &&
            rate_hz == static_cast<std::uint32_t>(kEsoModelRateHz) &&
            std::abs(nominal_inductance_h - kSimulinkInductanceH) < 1e-15) {
        input_gain = kEsoInputGain;
        current_gain = kEsoCurrentGain;
        disturbance_gain = kEsoDisturbanceGain;
        return;
    }

    // Preserve the observer bandwidth while adapting the plant input gain to
    // the capture interval and the configured hardware nominal inductance.
    // Resistance, dq cross-coupling, back EMF, dead-time and device drops are
    // treated as the ESO's lumped disturbance. For the correction/prediction
    // ordering emitted by R2024b, det(Ae)=1-L1=p^2 and B*L2=(1-p)^2.
    // The explicit reference branch above keeps the generated constants
    // bit-for-bit identical to the source model.
    const double safe_rate = static_cast<double>(
        std::max<std::uint32_t>(1U, rate_hz));
    const double reference_pole = std::sqrt(1.0 - kEsoCurrentGain);
    const double pole = std::pow(
        reference_pole, kEsoModelRateHz / safe_rate * bandwidth_rad_s / 4000.0);
    input_gain = 1.0 / (safe_rate * nominal_inductance_h);
    current_gain = 1.0 - pole * pole;
    disturbance_gain = (1.0 - pole) * (1.0 - pole) / input_gain;
}

void OnlineRlsEstimator::EsoAxis::reset() noexcept {
    state_current = 0.0;
    state_disturbance = 0.0;
    delayed_voltage = 0.0;
}

double OnlineRlsEstimator::EsoAxis::step(
        double measured_current, double applied_voltage) noexcept {
    // Generated R2024b order:
    //   xhat = state + L * (y - C*state)
    //   state+ = [1 B; 0 1] * xhat + [B;0] * u(k-1)
    // The returned current estimate is the corrected xhat(1), before the
    // prediction state and one-sample voltage delay are advanced.
    const double innovation = measured_current - state_current;
    const double current_hat = state_current +
        current_gain * innovation;
    const double disturbance_hat = state_disturbance +
        disturbance_gain * innovation;
    state_current = current_hat +
        input_gain * disturbance_hat +
        input_gain * delayed_voltage;
    state_disturbance = disturbance_hat;
    delayed_voltage = applied_voltage;
    return current_hat;
}

void OnlineRlsEstimator::set_enabled(bool enabled, bool reset_state) {
    std::lock_guard<std::mutex> lock(mutex_);
    enabled_ = enabled;
    if (reset_state) {
        reset_unlocked(rate_hz_);
    }
}

bool OnlineRlsEstimator::enabled() const noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    return enabled_;
}

void OnlineRlsEstimator::reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    reset_unlocked(rate_hz_);
}

void OnlineRlsEstimator::reset_unlocked(std::uint32_t rate_hz) {
    rate_hz_ = std::max<std::uint32_t>(1U, rate_hz);
    d_ = {};
    q_ = {};
    eso_d_.configure(rate_hz_, nominal_inductance_h_,
                     exact_simulink_reference_, bandwidth_rad_s_);
    eso_q_.configure(rate_hz_, nominal_inductance_h_,
                     exact_simulink_reference_, bandwidth_rad_s_);
    eso_d_.reset();
    eso_q_.reset();
    id_hat_a_ = 0.0;
    iq_hat_a_ = 0.0;
    id_prediction_a_ = iq_prediction_a_ = 0.0;
    innovation_d_ = innovation_q_ = 0.0;
    id_history_ = {};
    iq_history_ = {};
    vd_history_ = {};
    vq_history_ = {};
    primed_ = 0U;
    updates_ = 0U;
    samples_since_report_ = 0U;
    innovation_power_ema_ = 0.0;

    d_.theta[0] = 1.0;
    d_.theta[3] = eso_d_.input_gain;
    q_.theta[0] = 1.0;
    q_.theta[5] = eso_q_.input_gain;
    for (std::size_t i = 0; i < kTheta; ++i) {
        d_.covariance[i][i] = kP0;
        q_.covariance[i][i] = kP0;
    }
}

bool OnlineRlsEstimator::update_axis(
        Axis& axis, const std::array<double, kTheta>& regressor,
        double output, double& innovation) {
    std::array<double, kTheta> p_phi{};
    double prediction = 0.0;
    double denominator = forgetting_factor_;
    for (std::size_t i = 0; i < kTheta; ++i) {
        for (std::size_t j = 0; j < kTheta; ++j) {
            p_phi[i] += axis.covariance[i][j] * regressor[j];
        }
        prediction += regressor[i] * axis.theta[i];
        denominator += regressor[i] * p_phi[i];
    }
    if (!(denominator > 0.0) || !std::isfinite(denominator)) {
        return false;
    }

    innovation = output - prediction;
    if (!std::isfinite(innovation)) {
        return false;
    }
    if (!rls_adaptation_) {
        return true;
    }
    const double inverse_denominator = 1.0 / denominator;
    for (std::size_t i = 0; i < kTheta; ++i) {
        axis.theta[i] += p_phi[i] * inverse_denominator * innovation;
        if (!std::isfinite(axis.theta[i])) {
            return false;
        }
    }
    for (std::size_t i = 0; i < kTheta; ++i) {
        for (std::size_t j = 0; j < kTheta; ++j) {
            axis.covariance[i][j] -=
                p_phi[i] * p_phi[j] * inverse_denominator;
            axis.covariance[i][j] /= forgetting_factor_;
            if (!std::isfinite(axis.covariance[i][j])) {
                return false;
            }
        }
    }
    return true;
}

bool OnlineRlsEstimator::snapshot_unlocked(std::uint32_t tick_ms,
                                           RlsResult& result) const {
    result = {};
    result.tick_ms = tick_ms;
    result.updates = updates_;
    result.innov_rms_a = std::sqrt(std::max(0.0, innovation_power_ema_));
    result.id_hat_a = id_hat_a_;
    result.iq_hat_a = iq_hat_a_;
    result.id_prediction_a = id_prediction_a_;
    result.iq_prediction_a = iq_prediction_a_;
    result.innovation_d = innovation_d_;
    result.innovation_q = innovation_q_;
    double trace_d = 0.0;
    double trace_q = 0.0;
    for (std::size_t i = 0; i < kTheta; ++i) {
        result.theta_d[i] = d_.theta[i];
        result.theta_q[i] = q_.theta[i];
        trace_d += d_.covariance[i][i];
        trace_q += q_.covariance[i][i];
    }
    result.p_trace = std::max(trace_d, trace_q);
    return true;
}

bool OnlineRlsEstimator::ingest_dq_unlocked(
        const RlsDqInput& input, RlsResult& result, bool force_snapshot) {
    if (!enabled_ || !finite_dq_input(input)) {
        return false;
    }
    if (input.rate_hz != rate_hz_) {
        reset_unlocked(input.rate_hz);
    }

    // ESOrls routes the corrected ESO current estimates, not the noisy
    // measurements, into both the RLS output and its current-delay regressors.
    id_prediction_a_ = eso_d_.state_current;
    iq_prediction_a_ = eso_q_.state_current;
    id_hat_a_ = eso_d_.step(input.id_a, input.ud_v);
    iq_hat_a_ = eso_q_.step(input.iq_a, input.uq_v);

    // The source model has one voltage delay before the RLS subsystem and a
    // 1/2-sample pair inside it. Effective voltage lags are k-2 and k-3.
    if (rls_enabled_ && primed_ >= 1U) {
        const std::array<double, kTheta> phi_d{
            id_history_[0], id_history_[1], id_history_[2],
            vd_history_[1], vd_history_[2],
            vq_history_[1], vq_history_[2]};
        const std::array<double, kTheta> phi_q{
            iq_history_[0], iq_history_[1], iq_history_[2],
            vd_history_[1], vd_history_[2],
            vq_history_[1], vq_history_[2]};
        double innovation_d = 0.0;
        double innovation_q = 0.0;
        if (!update_axis(d_, phi_d, id_hat_a_, innovation_d) ||
                !update_axis(q_, phi_q, iq_hat_a_, innovation_q)) {
            return false;
        }
        innovation_d_ = innovation_d;
        innovation_q_ = innovation_q;
        const double power = innovation_d * innovation_d +
            innovation_q * innovation_q;
        innovation_power_ema_ += (power - innovation_power_ema_) / 1024.0;
        ++updates_;
    } else {
        ++primed_;
    }
    ++samples_since_report_;

    for (std::size_t i = kAr - 1U; i > 0U; --i) {
        id_history_[i] = id_history_[i - 1U];
        iq_history_[i] = iq_history_[i - 1U];
        vd_history_[i] = vd_history_[i - 1U];
        vq_history_[i] = vq_history_[i - 1U];
    }
    id_history_[0] = id_hat_a_;
    iq_history_[0] = iq_hat_a_;
    vd_history_[0] = input.ud_v;
    vq_history_[0] = input.uq_v;

    const auto report_samples = std::max<std::uint32_t>(1U, rate_hz_ / 10U);
    const bool report_due = updates_ > 0U &&
        samples_since_report_ >= report_samples;
    if (report_due) {
        samples_since_report_ = 0U;
    }
    if (force_snapshot || report_due) {
        return snapshot_unlocked(input.tick_ms, result);
    }
    return false;
}

bool OnlineRlsEstimator::ingest_dq(const RlsDqInput& input,
                                   RlsResult& result,
                                   bool force_snapshot) {
    std::lock_guard<std::mutex> lock(mutex_);
    return ingest_dq_unlocked(input, result, force_snapshot);
}

bool OnlineRlsEstimator::ingest(const RlsInput& input, RlsResult& result) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!enabled_ || !finite_f1_input(input)) {
        return false;
    }

    double id_a = input.id_a;
    if (!input.has_direct_id) {
        const double angle = input.angle_deg * kPi / 180.0;
        const double alpha = input.ia_a;
        const double beta = -(input.ia_a + 2.0 * input.ib_a) / kSqrt3;
        id_a = alpha * std::sin(angle) + beta * std::cos(angle);
    }
    const double volts_per_digit = input.vbus_v / (kSqrt3 * 32768.0);
    const double ud_v = input.has_applied_voltage
        ? input.vd_applied_v : input.vd_raw * volts_per_digit;
    const double uq_v = input.has_applied_voltage
        ? input.vq_applied_v : input.vq_raw * volts_per_digit;
    const RlsDqInput dq{
        input.tick_ms, input.rate_hz, id_a, input.iq_a,
        ud_v, uq_v};
    return ingest_dq_unlocked(dq, result, false);
}

}  // namespace motor_core
