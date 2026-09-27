#pragma once

#include "motor_core/telemetry_processor.hpp"

#include <array>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <vector>

namespace motor_core {

struct VectorTrailSnapshot {
    std::vector<double> current_x;
    std::vector<double> current_y;
    std::vector<double> flux_x;
    std::vector<double> flux_y;
    std::array<double, 2> current_tip{};
    std::array<double, 2> flux_tip{};
    bool has_current = false;
    bool has_flux = false;
    bool clarke_missing = false;
    std::uint64_t generation = 0;
};

// The hot F1 vector path stays in C++ from decoded samples through bounded
// trail storage. Python only receives four numeric arrays at UI refresh time.
class VectorTrail {
public:
    void configure(bool enabled, bool clarke, double psi_f, double lq);
    void clear();
    void append_samples(const std::vector<F1Sample>& samples);
    VectorTrailSnapshot snapshot(bool unlimited,
                                std::optional<double> spin_angle) const;

private:
    struct Point {
        double ix = 0.0;
        double iy = 0.0;
        double px = 0.0;
        double py = 0.0;
        bool current_valid = false;
    };

    mutable std::mutex mutex_;
    std::deque<Point> points_;
    bool enabled_ = false;
    bool clarke_ = false;
    bool clarke_missing_ = false;
    double psi_f_ = 0.0;
    double lq_ = 0.0;
    std::uint32_t rate_hz_ = 0;
    std::uint32_t phase_ = 0;
    std::uint64_t generation_ = 0;
};

}  // namespace motor_core
