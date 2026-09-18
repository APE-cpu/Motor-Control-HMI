#include "motor_core/online_rls.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace motor_core {
namespace {

constexpr double kNominalResistanceOhm = 0.59;
constexpr double kNominalInductanceH = 0.00066;
constexpr double kCurrentScaleA = 5.0;
constexpr double kVoltageScaleV = 24.0;
constexpr double kP0 = 100.0;
constexpr double kPMax = 1.0e8;
constexpr double kCovarianceNoise = 1.0e-10;
constexpr double kThetaMax = 1.0e4;
constexpr double kSqrt3 = 1.7320508075688772935;
constexpr double kPi = 3.14159265358979323846;

bool finite_input(const RlsInput& input) {
    return input.rate_hz > 0U && input.vbus_v > 1.0 &&
        std::isfinite(input.angle_deg) && std::isfinite(input.iq_a) &&
        std::isfinite(input.ia_a) && std::isfinite(input.ib_a) &&
        std::isfinite(input.vd_raw) && std::isfinite(input.vq_raw) &&
        std::isfinite(input.vbus_v);
}

}  // namespace

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
    eso_d_ = {};
    eso_q_ = {};
    id_history_ = {};
    iq_history_ = {};
    vd_history_ = {};
    vq_history_ = {};
    primed_ = 0U;
    updates_ = 0U;
    samples_since_report_ = 0U;
    innovation_power_ema_ = 0.0;

    const double sample_time = 1.0 / static_cast<double>(rate_hz_);
    const double a = std::exp(
        -kNominalResistanceOhm * sample_time / kNominalInductanceH);
    const double b_si = (1.0 - a) / kNominalResistanceOhm;
    const double b_internal = b_si * kVoltageScaleV / kCurrentScaleA;
    d_.theta[0] = a;
    q_.theta[0] = a;
    d_.theta[3] = b_internal;
    q_.theta[5] = b_internal;
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
    double denominator = 1.0;
    for (std::size_t i = 0; i < kTheta; ++i) {
        for (std::size_t j = 0; j < kTheta; ++j) {
            p_phi[i] += axis.covariance[i][j] * regressor[j];
        }
        prediction += regressor[i] * axis.theta[i];
        denominator += regressor[i] * p_phi[i];
    }
    if (!(denominator > 1.0e-15) || !std::isfinite(denominator)) {
        return false;
    }

    innovation = output - prediction;
    const double inverse_denominator = 1.0 / denominator;
    for (std::size_t i = 0; i < kTheta; ++i) {
        axis.theta[i] += p_phi[i] * inverse_denominator * innovation;
        if (!std::isfinite(axis.theta[i]) ||
                std::abs(axis.theta[i]) > kThetaMax) {
            return false;
        }
    }
    for (std::size_t i = 0; i < kTheta; ++i) {
        for (std::size_t j = 0; j < kTheta; ++j) {
            axis.covariance[i][j] -=
                p_phi[i] * p_phi[j] * inverse_denominator;
        }
    }

    double trace = 0.0;
    for (std::size_t i = 0; i < kTheta; ++i) {
        for (std::size_t j = i + 1; j < kTheta; ++j) {
            const double symmetric = 0.5 *
                (axis.covariance[i][j] + axis.covariance[j][i]);
            axis.covariance[i][j] = symmetric;
            axis.covariance[j][i] = symmetric;
        }
        axis.covariance[i][i] = std::max(
            0.0, axis.covariance[i][i]) + kCovarianceNoise;
        trace += axis.covariance[i][i];
    }
    if (!std::isfinite(trace) || trace > kPMax) {
        return false;
    }
    return std::isfinite(innovation);
}

double OnlineRlsEstimator::update_eso(EsoAxis& eso, double measured_a,
                                      double voltage_v) const {
    if (!eso.primed) {
        eso.x_hat_a = measured_a;
        eso.previous_voltage_v = voltage_v;
        eso.primed = true;
        return measured_a;
    }

    const double sample_time = 1.0 / static_cast<double>(rate_hz_);
    // Keep the observer comfortably below Nyquist at reduced F1 rates.
    const double observer_rad_s = std::min(
        4000.0, 0.15 * 2.0 * kPi * static_cast<double>(rate_hz_));
    const double pole = std::exp(-observer_rad_s * sample_time);
    const double l1 = 1.0 - pole * pole;
    const double l2 = (1.0 - pole) * (1.0 - pole) / sample_time;
    const double nominal_b = sample_time / kNominalInductanceH;
    const double prediction = eso.x_hat_a +
        nominal_b * eso.previous_voltage_v +
        sample_time * eso.f_hat_a_s;
    const double error = measured_a - prediction;
    eso.x_hat_a = prediction + l1 * error;
    eso.f_hat_a_s += l2 * error;
    eso.previous_voltage_v = voltage_v;
    if (!std::isfinite(eso.x_hat_a) || !std::isfinite(eso.f_hat_a_s) ||
            std::abs(eso.x_hat_a) > 20.0 ||
            std::abs(eso.f_hat_a_s) > 100000.0) {
        eso.x_hat_a = measured_a;
        eso.f_hat_a_s = 0.0;
        eso.previous_voltage_v = voltage_v;
    }
    return eso.x_hat_a;
}

bool OnlineRlsEstimator::snapshot_unlocked(std::uint32_t tick_ms,
                                           RlsResult& result) const {
    result = {};
    result.tick_ms = tick_ms;
    result.updates = updates_;
    result.innov_rms_a = std::sqrt(std::max(0.0, innovation_power_ema_));
    double trace_d = 0.0;
    double trace_q = 0.0;
    for (std::size_t i = 0; i < kTheta; ++i) {
        result.theta_d[i] = d_.theta[i];
        result.theta_q[i] = q_.theta[i];
        trace_d += d_.covariance[i][i];
        trace_q += q_.covariance[i][i];
    }
    // Convert normalized voltage coefficients back to A/V.
    for (std::size_t i = kAr; i < kTheta; ++i) {
        result.theta_d[i] *= kCurrentScaleA / kVoltageScaleV;
        result.theta_q[i] *= kCurrentScaleA / kVoltageScaleV;
    }
    result.p_trace = std::max(trace_d, trace_q);
    return true;
}

bool OnlineRlsEstimator::ingest(const RlsInput& input, RlsResult& result) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!enabled_ || !finite_input(input)) {
        return false;
    }
    if (input.rate_hz != rate_hz_) {
        reset_unlocked(input.rate_hz);
    }

    // Match ST MCSDK exactly:
    // beta=-(Ia+2Ib)/sqrt(3), q=alpha*cos-beta*sin,
    // d=alpha*sin+beta*cos.
    const double angle = input.angle_deg * kPi / 180.0;
    const double alpha = input.ia_a;
    const double beta = -(input.ia_a + 2.0 * input.ib_a) / kSqrt3;
    const double id_measured = alpha * std::sin(angle) + beta * std::cos(angle);
    const double iq_measured = alpha * std::cos(angle) - beta * std::sin(angle);
    const double volts_per_digit = input.vbus_v / (kSqrt3 * 32768.0);
    const double vd_v = input.vd_raw * volts_per_digit;
    const double vq_v = input.vq_raw * volts_per_digit;
    const double id = update_eso(eso_d_, id_measured, vd_v) / kCurrentScaleA;
    const double iq = update_eso(eso_q_, iq_measured, vq_v) / kCurrentScaleA;
    const double vd = vd_v / kVoltageScaleV;
    const double vq = vq_v / kVoltageScaleV;

    if (primed_ >= kAr + 1U) {
        const std::array<double, kTheta> phi_d{
            id_history_[0], id_history_[1], id_history_[2],
            vd_history_[0], vd_history_[1],
            vq_history_[0], vq_history_[1]};
        const std::array<double, kTheta> phi_q{
            iq_history_[0], iq_history_[1], iq_history_[2],
            vd_history_[0], vd_history_[1],
            vq_history_[0], vq_history_[1]};
        double innovation_d = 0.0;
        double innovation_q = 0.0;
        if (!update_axis(d_, phi_d, id, innovation_d) ||
                !update_axis(q_, phi_q, iq, innovation_q)) {
            reset_unlocked(rate_hz_);
            return false;
        }
        innovation_d *= kCurrentScaleA;
        innovation_q *= kCurrentScaleA;
        const double power = innovation_d * innovation_d +
            innovation_q * innovation_q;
        innovation_power_ema_ += (power - innovation_power_ema_) / 1024.0;
        ++updates_;
        ++samples_since_report_;
    } else {
        ++primed_;
    }

    for (std::size_t i = kAr - 1U; i > 0U; --i) {
        id_history_[i] = id_history_[i - 1U];
        iq_history_[i] = iq_history_[i - 1U];
    }
    id_history_[0] = id;
    iq_history_[0] = iq;
    vd_history_[1] = vd_history_[0];
    vq_history_[1] = vq_history_[0];
    vd_history_[0] = vd;
    vq_history_[0] = vq;

    const auto report_samples = std::max<std::uint32_t>(1U, rate_hz_ / 10U);
    if (updates_ > 0U && samples_since_report_ >= report_samples) {
        samples_since_report_ = 0U;
        return snapshot_unlocked(input.tick_ms, result);
    }
    return false;
}

}  // namespace motor_core
