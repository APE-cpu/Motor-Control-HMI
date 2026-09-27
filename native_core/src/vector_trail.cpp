#include "motor_core/vector_trail.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace motor_core {
namespace {
constexpr double kPi = 3.14159265358979323846;
constexpr double kSqrt3 = 1.7320508075688772935;
constexpr std::size_t kTrail = 800;
constexpr std::size_t kTrailMax = 4000;
constexpr std::size_t kSpinSearch = 200;

std::array<double, 2> select_tip(const std::vector<double>& xs,
                                 const std::vector<double>& ys,
                                 std::optional<double> spin_angle) {
    if (xs.empty()) {
        return {};
    }
    std::size_t best_index = xs.size() - 1;
    if (spin_angle) {
        double best_error = std::numeric_limits<double>::infinity();
        const auto first = xs.size() > kSpinSearch ? xs.size() - kSpinSearch : 0;
        for (std::size_t index = first; index < xs.size(); ++index) {
            const double phase = std::atan2(ys[index], xs[index]);
            const double error = std::abs(std::remainder(
                phase - *spin_angle, 2.0 * kPi));
            if (error < best_error) {
                best_error = error;
                best_index = index;
            }
        }
    }
    return {xs[best_index], ys[best_index]};
}
}  // namespace

void VectorTrail::configure(bool enabled, bool clarke,
                            double psi_f, double lq) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (clarke_ != clarke) {
        points_.clear();
        clarke_missing_ = false;
        ++generation_;
    }
    enabled_ = enabled;
    clarke_ = clarke;
    psi_f_ = psi_f;
    lq_ = lq;
}

void VectorTrail::clear() {
    std::lock_guard<std::mutex> lock(mutex_);
    points_.clear();
    clarke_missing_ = false;
    rate_hz_ = 0;
    phase_ = 0;
    ++generation_;
}

void VectorTrail::append_samples(const std::vector<F1Sample>& samples) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!enabled_) {
        return;
    }
    for (const auto& sample : samples) {
        const auto rate = std::max<std::uint32_t>(sample.rate_hz, 1U);
        const auto stride = std::max<std::uint32_t>(1U, (rate + 999U) / 1000U);
        if (rate_hz_ != rate) {
            rate_hz_ = rate;
            phase_ = 0;
        }
        const bool select = phase_ == 0;
        phase_ = (phase_ + 1U) % stride;
        if (!select) {
            continue;
        }
        const double theta = sample.angle_deg * kPi / 180.0;
        const double s = std::sin(theta);
        const double c = std::cos(theta);
        const double iq = sample.iq_a;
        const double psi_q = lq_ * iq;
        Point point;
        point.px = psi_f_ * c - psi_q * s;
        point.py = psi_f_ * s + psi_q * c;
        if (clarke_) {
            point.current_valid = sample.has_phase_current;
            if (point.current_valid) {
                point.ix = sample.ia_a;
                // Match the vector page's amplitude-invariant Clarke view.
                point.iy = (sample.ia_a + 2.0 * sample.ib_a) / kSqrt3;
            }
            clarke_missing_ = !point.current_valid;
        } else {
            point.current_valid = true;
            point.ix = -iq * s;
            point.iy = iq * c;
            clarke_missing_ = false;
        }
        points_.push_back(point);
        if (points_.size() > kTrailMax) {
            points_.pop_front();
        }
        ++generation_;
    }
}

VectorTrailSnapshot VectorTrail::snapshot(
        bool unlimited, std::optional<double> spin_angle) const {
    std::lock_guard<std::mutex> lock(mutex_);
    VectorTrailSnapshot result;
    result.generation = generation_;
    result.clarke_missing = clarke_ && clarke_missing_;
    const auto count = std::min(points_.size(), unlimited ? kTrailMax : kTrail);
    const auto first = points_.size() - count;
    result.current_x.reserve(count);
    result.current_y.reserve(count);
    result.flux_x.reserve(count);
    result.flux_y.reserve(count);
    for (std::size_t index = first; index < points_.size(); ++index) {
        const auto& point = points_[index];
        if (point.current_valid) {
            result.current_x.push_back(point.ix);
            result.current_y.push_back(point.iy);
        }
        result.flux_x.push_back(point.px);
        result.flux_y.push_back(point.py);
    }
    result.has_current = !result.current_x.empty();
    result.has_flux = !result.flux_x.empty();
    result.current_tip = select_tip(
        result.current_x, result.current_y, spin_angle);
    result.flux_tip = select_tip(result.flux_x, result.flux_y, spin_angle);
    return result;
}

}  // namespace motor_core
