#include "motor_core/protocol_v2.hpp"
#include "motor_core/telemetry_processor.hpp"
#include "motor_core/tcp_v2_receiver.hpp"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <cmath>
#include <string>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace {

py::list frames_to_python(const std::vector<motor_core::Frame>& frames) {
    py::list output;
    for (const auto& frame : frames) {
        output.append(py::make_tuple(
            frame.version, frame.address, frame.sequence, frame.message_type,
            frame.command,
            py::bytes(reinterpret_cast<const char*>(frame.payload.data()),
                      frame.payload.size())));
    }
    return output;
}

py::list f1_samples_to_python(
        const std::vector<motor_core::F1Sample>& samples) {
    // Reuse interned keys: PyDict_SetItemString would allocate/look up the same
    // Unicode keys for every high-rate sample crossing the Python boundary.
    static PyObject* tick_ms_key = PyUnicode_InternFromString("tick_ms");
    static PyObject* sample_seq_key = PyUnicode_InternFromString("sample_seq");
    static PyObject* rate_hz_key = PyUnicode_InternFromString("rate_hz");
    static PyObject* angle_deg_key = PyUnicode_InternFromString("angle_deg");
    static PyObject* actuation_angle_deg_key =
        PyUnicode_InternFromString("actuation_angle_deg");
    static PyObject* speed_rpm_key = PyUnicode_InternFromString("speed_rpm");
    static PyObject* iq_a_key = PyUnicode_InternFromString("iq_a");
    static PyObject* id_a_key = PyUnicode_InternFromString("id_a");
    static PyObject* iqref_a_key = PyUnicode_InternFromString("iqref_a");
    static PyObject* idref_a_key = PyUnicode_InternFromString("idref_a");
    static PyObject* ia_a_key = PyUnicode_InternFromString("ia_a");
    static PyObject* ib_a_key = PyUnicode_InternFromString("ib_a");
    static PyObject* vd_raw_key = PyUnicode_InternFromString("vd_raw");
    static PyObject* vq_raw_key = PyUnicode_InternFromString("vq_raw");
    static PyObject* vbus_v_key = PyUnicode_InternFromString("vbus_v");
    static PyObject* vdda_v_key = PyUnicode_InternFromString("vdda_v");
    static PyObject* duty_a_key = PyUnicode_InternFromString("duty_a");
    static PyObject* duty_b_key = PyUnicode_InternFromString("duty_b");
    static PyObject* duty_c_key = PyUnicode_InternFromString("duty_c");
    static PyObject* vd_applied_v_key =
        PyUnicode_InternFromString("vd_applied_v");
    static PyObject* vq_applied_v_key =
        PyUnicode_InternFromString("vq_applied_v");
    if (tick_ms_key == nullptr || sample_seq_key == nullptr ||
        rate_hz_key == nullptr ||
        angle_deg_key == nullptr || actuation_angle_deg_key == nullptr ||
        speed_rpm_key == nullptr ||
        iq_a_key == nullptr || id_a_key == nullptr ||
        iqref_a_key == nullptr || idref_a_key == nullptr ||
        ia_a_key == nullptr ||
        ib_a_key == nullptr || vd_raw_key == nullptr || vq_raw_key == nullptr ||
        vbus_v_key == nullptr || vdda_v_key == nullptr ||
        duty_a_key == nullptr || duty_b_key == nullptr || duty_c_key == nullptr ||
        vd_applied_v_key == nullptr || vq_applied_v_key == nullptr) {
        throw py::error_already_set();
    }
    py::list output(samples.size());
    for (std::size_t index = 0; index < samples.size(); ++index) {
        const auto& sample = samples[index];
        PyObject* item = PyDict_New();
        if (item == nullptr) {
            throw py::error_already_set();
        }
        const auto set_item = [item](PyObject* key, PyObject* value) {
            if (value == nullptr) {
                Py_DECREF(item);
                throw py::error_already_set();
            }
            const int result = PyDict_SetItem(item, key, value);
            Py_DECREF(value);
            if (result != 0) {
                Py_DECREF(item);
                throw py::error_already_set();
            }
        };
        set_item(tick_ms_key, PyLong_FromUnsignedLong(sample.tick_ms));
        if (sample.has_sample_sequence) {
            set_item(sample_seq_key,
                     PyLong_FromUnsignedLong(sample.sample_sequence));
        }
        set_item(rate_hz_key, PyLong_FromUnsignedLong(sample.rate_hz));
        set_item(angle_deg_key, PyFloat_FromDouble(sample.angle_deg));
        if (sample.has_applied_voltage) {
            set_item(actuation_angle_deg_key,
                     PyFloat_FromDouble(sample.actuation_angle_deg));
        }
        set_item(speed_rpm_key, PyFloat_FromDouble(sample.speed_rpm));
        set_item(iq_a_key, PyFloat_FromDouble(sample.iq_a));
        if (sample.has_direct_dq_current) {
            set_item(id_a_key, PyFloat_FromDouble(sample.id_a));
        }
        set_item(iqref_a_key, PyFloat_FromDouble(sample.iqref_a));
        if (sample.has_direct_dq_current) {
            set_item(idref_a_key, PyFloat_FromDouble(sample.idref_a));
        }
        if (sample.has_phase_current) {
            set_item(ia_a_key, PyFloat_FromDouble(sample.ia_a));
            set_item(ib_a_key, PyFloat_FromDouble(sample.ib_a));
        }
        if (sample.has_voltage) {
            set_item(vd_raw_key, PyFloat_FromDouble(sample.vd_raw));
            set_item(vq_raw_key, PyFloat_FromDouble(sample.vq_raw));
            set_item(vbus_v_key, PyFloat_FromDouble(sample.vbus_v));
        }
        if (sample.has_vdda) {
            set_item(vdda_v_key, PyFloat_FromDouble(sample.vdda_v));
        }
        if (sample.has_applied_voltage) {
            set_item(duty_a_key, PyLong_FromUnsignedLong(sample.duty_a));
            set_item(duty_b_key, PyLong_FromUnsignedLong(sample.duty_b));
            set_item(duty_c_key, PyLong_FromUnsignedLong(sample.duty_c));
            set_item(vd_applied_v_key,
                     PyFloat_FromDouble(sample.vd_applied_v));
            set_item(vq_applied_v_key,
                     PyFloat_FromDouble(sample.vq_applied_v));
        }
        PyList_SET_ITEM(output.ptr(), static_cast<Py_ssize_t>(index), item);
    }
    return output;
}

py::dict f1_samples_to_columns(
        const std::vector<motor_core::F1Sample>& samples) {
    const auto size = static_cast<Py_ssize_t>(samples.size());
    py::list tick_ms(size);
    py::list sample_seq(size);
    py::list angle_deg(size);
    py::list actuation_angle_deg(size);
    py::list speed_rpm(size);
    py::list iq_a(size);
    py::list id_a(size);
    py::list iqref_a(size);
    py::list idref_a(size);
    py::list ia_a(size);
    py::list ib_a(size);
    py::list vd_raw(size);
    py::list vq_raw(size);
    py::list vbus_v(size);
    py::list vdda_v(size);
    py::list duty_a(size);
    py::list duty_b(size);
    py::list duty_c(size);
    py::list vd_applied_v(size);
    py::list vq_applied_v(size);
    const auto set_item = [](py::list& column, Py_ssize_t index,
                             PyObject* value) {
        if (value == nullptr) {
            throw py::error_already_set();
        }
        PyList_SET_ITEM(column.ptr(), index, value);
    };
    std::uint32_t rate_hz = 1000;
    bool direct_id = !samples.empty();
    bool direct_sequence = !samples.empty();
    bool direct_vdda = !samples.empty();
    bool direct_applied_voltage = !samples.empty();
    for (Py_ssize_t index = 0; index < size; ++index) {
        const auto& sample = samples[static_cast<std::size_t>(index)];
        rate_hz = sample.rate_hz;
        set_item(tick_ms, index, PyLong_FromUnsignedLong(sample.tick_ms));
        set_item(sample_seq, index,
                 PyLong_FromUnsignedLong(sample.sample_sequence));
        direct_sequence = direct_sequence && sample.has_sample_sequence;
        set_item(angle_deg, index, PyFloat_FromDouble(sample.angle_deg));
        set_item(actuation_angle_deg, index,
                 PyFloat_FromDouble(sample.actuation_angle_deg));
        set_item(speed_rpm, index, PyFloat_FromDouble(sample.speed_rpm));
        set_item(iq_a, index, PyFloat_FromDouble(sample.iq_a));
        double exported_id = sample.id_a;
        if (!sample.has_direct_dq_current) {
            constexpr double kPi = 3.14159265358979323846;
            constexpr double kSqrt3 = 1.7320508075688772935;
            const double angle = sample.angle_deg * kPi / 180.0;
            const double beta = -(sample.ia_a + 2.0 * sample.ib_a) / kSqrt3;
            exported_id = sample.ia_a * std::sin(angle) +
                beta * std::cos(angle);
            direct_id = false;
        }
        set_item(id_a, index, PyFloat_FromDouble(exported_id));
        set_item(iqref_a, index, PyFloat_FromDouble(sample.iqref_a));
        set_item(idref_a, index, PyFloat_FromDouble(sample.idref_a));
        set_item(ia_a, index, PyFloat_FromDouble(
            sample.has_phase_current ? sample.ia_a : 0.0));
        set_item(ib_a, index, PyFloat_FromDouble(
            sample.has_phase_current ? sample.ib_a : 0.0));
        set_item(vd_raw, index, PyFloat_FromDouble(
            sample.has_voltage ? sample.vd_raw : 0.0));
        set_item(vq_raw, index, PyFloat_FromDouble(
            sample.has_voltage ? sample.vq_raw : 0.0));
        set_item(vbus_v, index, PyFloat_FromDouble(
            sample.has_voltage ? sample.vbus_v : 0.0));
        set_item(vdda_v, index, PyFloat_FromDouble(
            sample.has_vdda ? sample.vdda_v : 0.0));
        set_item(duty_a, index, PyLong_FromUnsignedLong(sample.duty_a));
        set_item(duty_b, index, PyLong_FromUnsignedLong(sample.duty_b));
        set_item(duty_c, index, PyLong_FromUnsignedLong(sample.duty_c));
        set_item(vd_applied_v, index,
                 PyFloat_FromDouble(sample.vd_applied_v));
        set_item(vq_applied_v, index,
                 PyFloat_FromDouble(sample.vq_applied_v));
        direct_vdda = direct_vdda && sample.has_vdda;
        direct_applied_voltage = direct_applied_voltage &&
            sample.has_applied_voltage;
    }
    py::dict output;
    output["count"] = samples.size();
    output["rate_hz"] = rate_hz;
    output["tick_ms"] = std::move(tick_ms);
    output["sample_seq"] = std::move(sample_seq);
    output["sequence_source_direct"] = direct_sequence;
    output["angle_deg"] = std::move(angle_deg);
    output["actuation_angle_deg"] = std::move(actuation_angle_deg);
    output["speed_rpm"] = std::move(speed_rpm);
    output["iq_a"] = std::move(iq_a);
    output["id_a"] = std::move(id_a);
    output["id_source_direct"] = direct_id;
    output["iqref_a"] = std::move(iqref_a);
    output["idref_a"] = std::move(idref_a);
    output["ia_a"] = std::move(ia_a);
    output["ib_a"] = std::move(ib_a);
    output["vd_raw"] = std::move(vd_raw);
    output["vq_raw"] = std::move(vq_raw);
    output["vbus_v"] = std::move(vbus_v);
    output["vdda_v"] = std::move(vdda_v);
    output["vdda_source_direct"] = direct_vdda;
    output["duty_a"] = std::move(duty_a);
    output["duty_b"] = std::move(duty_b);
    output["duty_c"] = std::move(duty_c);
    output["vd_applied_v"] = std::move(vd_applied_v);
    output["vq_applied_v"] = std::move(vq_applied_v);
    output["applied_voltage_source_direct"] = direct_applied_voltage;
    return output;
}

py::list bursts_to_python(
        const std::vector<motor_core::BurstCapture>& captures) {
    py::list output;
    for (const auto& capture : captures) {
        py::dict item;
        item["n"] = capture.ia.size();
        item["ia"] = capture.ia;
        item["ib"] = capture.ib;
        item["ang"] = capture.angle;
        output.append(std::move(item));
    }
    return output;
}

py::list f2_samples_to_python(
        const std::vector<motor_core::F2Sample>& samples) {
    py::list output;
    for (const auto& sample : samples) {
        py::dict item;
        item["tick_ms"] = sample.tick_ms;
        item["adc1_raw"] = sample.adc1_raw;
        item["adc2_raw"] = sample.adc2_raw;
        item["offset_a"] = sample.offset_a;
        item["offset_b"] = sample.offset_b;
        item["sector"] = sample.sector;
        item["duty_a"] = sample.duty_a;
        item["duty_b"] = sample.duty_b;
        item["duty_c"] = sample.duty_c;
        item["sample_point"] = sample.sample_point;
        item["adc1_v"] = sample.adc1_v;
        item["adc2_v"] = sample.adc2_v;
        item["zero_a_v"] = sample.zero_a_v;
        item["zero_b_v"] = sample.zero_b_v;
        item["adc1_delta_a"] = sample.adc1_delta_a;
        item["adc2_delta_a"] = sample.adc2_delta_a;
        if (sample.has_calibration) {
            item["cal_adc1_min"] = sample.cal_adc1_min;
            item["cal_adc1_max"] = sample.cal_adc1_max;
            item["cal_adc2_min"] = sample.cal_adc2_min;
            item["cal_adc2_max"] = sample.cal_adc2_max;
            item["cal_adc1_pp"] = sample.cal_adc1_max - sample.cal_adc1_min;
            item["cal_adc2_pp"] = sample.cal_adc2_max - sample.cal_adc2_min;
        }
        if (sample.has_vdda) {
            item["vdda_v"] = sample.vdda_v;
        }
        output.append(std::move(item));
    }
    return output;
}

py::list f3_samples_to_python(
        const std::vector<motor_core::F3Sample>& samples) {
    py::list output;
    for (const auto& sample : samples) {
        py::dict item;
        py::tuple theta_d(sample.theta_d.size());
        py::tuple theta_q(sample.theta_q.size());
        for (std::size_t index = 0; index < sample.theta_d.size(); ++index) {
            theta_d[index] = sample.theta_d[index];
            theta_q[index] = sample.theta_q[index];
        }
        item["tick_ms"] = sample.tick_ms;
        item["updates"] = sample.updates;
        item["innov_rms_a"] = sample.innov_rms_a;
        item["innov_rms_digit"] = sample.innov_rms_digit;
        item["p_trace"] = sample.p_trace;
        item["id_hat_a"] = sample.id_hat_a;
        item["iq_hat_a"] = sample.iq_hat_a;
        item["theta_d"] = std::move(theta_d);
        item["theta_q"] = std::move(theta_q);
        item["a1_d"] = sample.a1_d;
        item["a1_q"] = sample.a1_q;
        item["b_dd0_si"] = sample.b_dd0_si;
        item["b_qq0_si"] = sample.b_qq0_si;
        item["ld_mh"] = sample.ld_mh;
        item["lq_mh"] = sample.lq_mh;
        item["rd_ohm"] = sample.rd_ohm;
        item["rq_ohm"] = sample.rq_ohm;
        item["rl_equivalent_method"] = "arx3_low_frequency_moment";
        item["rl_physical_validated"] = false;
        output.append(std::move(item));
    }
    return output;
}

py::dict rls_result_to_python(const motor_core::RlsResult& sample,
                              std::size_t sample_index) {
    py::dict item;
    item["sample_index"] = sample_index;
    item["tick_ms"] = sample.tick_ms;
    item["updates"] = sample.updates;
    item["innov_rms_a"] = sample.innov_rms_a;
    item["p_trace"] = sample.p_trace;
    item["id_hat_a"] = sample.id_hat_a;
    item["iq_hat_a"] = sample.iq_hat_a;
    item["theta_d"] = sample.theta_d;
    item["theta_q"] = sample.theta_q;
    item["a1_d"] = sample.theta_d[0];
    item["a1_q"] = sample.theta_q[0];
    item["b_dd0_si"] = sample.theta_d[3];
    item["b_qq0_si"] = sample.theta_q[5];
    return item;
}

py::dict telemetry_stats_to_python(
        const motor_core::TelemetryProcessorStats& stats) {
    py::dict output;
    output["f1_frames"] = stats.f1_frames;
    output["f2_frames"] = stats.f2_frames;
    output["f3_frames"] = stats.f3_frames;
    output["f4_frames"] = stats.f4_frames;
    output["f1_samples"] = stats.f1_samples;
    output["completed_bursts"] = stats.completed_bursts;
    output["parse_errors"] = stats.parse_errors;
    output["dropped_samples"] = stats.dropped_samples;
    output["dropped_bursts"] = stats.dropped_bursts;
    output["dropped_diagnostics"] = stats.dropped_diagnostics;
    output["queued_samples"] = stats.queued_samples;
    output["queued_f2"] = stats.queued_f2;
    output["queued_f3"] = stats.queued_f3;
    output["queued_bursts"] = stats.queued_bursts;
    return output;
}

}  // namespace

PYBIND11_MODULE(motor_core_cpp, module) {
    module.doc() = "Native protocol and streaming primitives for Motor Control HMI";
    module.attr("__version__") = MOTOR_CORE_VERSION;
    // Increment when the Python/native telemetry contract changes.  The host
    // checks this independently from the package version so a stale .pyd can
    // never silently parse a new F1 wire layout with an older ABI.
    module.attr("telemetry_schema_version") = 3;

    module.def(
        "crc16_ccitt",
        [](const py::bytes& value, std::uint16_t initial) {
            const std::string data = value;
            return motor_core::crc16_ccitt(
                reinterpret_cast<const std::uint8_t*>(data.data()), data.size(),
                initial);
        },
        py::arg("data"), py::arg("initial") = 0xFFFF);

    module.def(
        "run_simulink_rls",
        [](const std::vector<double>& id_a,
           const std::vector<double>& iq_a,
           const std::vector<double>& ud_v,
           const std::vector<double>& uq_v,
           std::uint32_t rate_hz,
           std::size_t report_every,
           double nominal_inductance_h,
           bool exact_simulink_reference) {
            const std::size_t count = id_a.size();
            if (iq_a.size() != count || ud_v.size() != count ||
                    uq_v.size() != count) {
                throw py::value_error("RLS dq input columns must align");
            }
            if (rate_hz == 0U || report_every == 0U) {
                throw py::value_error("rate_hz and report_every must be positive");
            }
            motor_core::OnlineRlsEstimator estimator(
                nominal_inductance_h, exact_simulink_reference);
            estimator.set_enabled(true, true);
            std::vector<std::pair<std::size_t, motor_core::RlsResult>> results;
            results.reserve(count / report_every + 2U);
            {
                py::gil_scoped_release release;
                for (std::size_t index = 0; index < count; ++index) {
                    motor_core::RlsResult result;
                    const bool force_snapshot = report_every == 1U ||
                        (index + 1U) % report_every == 0U ||
                        index + 1U == count;
                    const auto tick_ms = static_cast<std::uint32_t>(
                        (static_cast<std::uint64_t>(index) * 1000U) / rate_hz);
                    const motor_core::RlsDqInput input{
                        tick_ms, rate_hz, id_a[index], iq_a[index],
                        ud_v[index], uq_v[index]};
                    if (estimator.ingest_dq(input, result, force_snapshot)) {
                        results.emplace_back(index, std::move(result));
                    }
                }
            }
            py::list output;
            for (const auto& entry : results) {
                output.append(rls_result_to_python(entry.second, entry.first));
            }
            return output;
        },
        py::arg("id_a"), py::arg("iq_a"), py::arg("ud_v"),
        py::arg("uq_v"), py::arg("rate_hz"),
        py::arg("report_every") = 1U,
        py::arg("nominal_inductance_h") = 19.36e-3,
        py::arg("exact_simulink_reference") = true);

    module.def(
        "run_simulink_rls_analysis",
        [](const std::vector<double>& id_a,
           const std::vector<double>& iq_a,
           const std::vector<double>& ud_v,
           const std::vector<double>& uq_v,
           std::uint32_t rate_hz,
           std::size_t report_every,
           std::size_t max_trace_points,
           double nominal_inductance_h,
           bool exact_simulink_reference) {
            const std::size_t count = id_a.size();
            if (iq_a.size() != count || ud_v.size() != count ||
                    uq_v.size() != count) {
                throw py::value_error("RLS dq input columns must align");
            }
            if (rate_hz == 0U || report_every == 0U ||
                    max_trace_points == 0U) {
                throw py::value_error(
                    "rate_hz, report_every and max_trace_points must be positive");
            }
            const std::size_t trace_stride = std::max<std::size_t>(
                1U, (count + max_trace_points - 1U) / max_trace_points);
            motor_core::OnlineRlsEstimator estimator(
                nominal_inductance_h, exact_simulink_reference);
            estimator.set_enabled(true, true);
            std::vector<std::pair<std::size_t, motor_core::RlsResult>> results;
            std::vector<std::size_t> trace_indices;
            std::vector<double> trace_id;
            std::vector<double> trace_iq;
            std::vector<double> trace_id_hat;
            std::vector<double> trace_iq_hat;
            results.reserve(count / report_every + 2U);
            trace_indices.reserve(std::min(count, max_trace_points) + 1U);
            trace_id.reserve(trace_indices.capacity());
            trace_iq.reserve(trace_indices.capacity());
            trace_id_hat.reserve(trace_indices.capacity());
            trace_iq_hat.reserve(trace_indices.capacity());
            {
                py::gil_scoped_release release;
                for (std::size_t index = 0; index < count; ++index) {
                    const bool report_sample =
                        (index + 1U) % report_every == 0U ||
                        index + 1U == count;
                    const bool trace_sample = index % trace_stride == 0U ||
                        index + 1U == count;
                    motor_core::RlsResult result;
                    const auto tick_ms = static_cast<std::uint32_t>(
                        (static_cast<std::uint64_t>(index) * 1000U) / rate_hz);
                    const motor_core::RlsDqInput input{
                        tick_ms, rate_hz, id_a[index], iq_a[index],
                        ud_v[index], uq_v[index]};
                    if (!estimator.ingest_dq(
                            input, result, report_sample || trace_sample)) {
                        continue;
                    }
                    if (report_sample) {
                        results.emplace_back(index, result);
                    }
                    if (trace_sample) {
                        trace_indices.push_back(index);
                        trace_id.push_back(id_a[index]);
                        trace_iq.push_back(iq_a[index]);
                        trace_id_hat.push_back(result.id_hat_a);
                        trace_iq_hat.push_back(result.iq_hat_a);
                    }
                }
            }
            py::list result_items;
            for (const auto& entry : results) {
                result_items.append(
                    rls_result_to_python(entry.second, entry.first));
            }
            py::dict trace;
            trace["sample_index"] = std::move(trace_indices);
            trace["id_a"] = std::move(trace_id);
            trace["iq_a"] = std::move(trace_iq);
            trace["id_hat_a"] = std::move(trace_id_hat);
            trace["iq_hat_a"] = std::move(trace_iq_hat);
            py::dict output;
            output["results"] = std::move(result_items);
            output["trace"] = std::move(trace);
            output["trace_stride"] = trace_stride;
            return output;
        },
        py::arg("id_a"), py::arg("iq_a"), py::arg("ud_v"),
        py::arg("uq_v"), py::arg("rate_hz"),
        py::arg("report_every") = 1U,
        py::arg("max_trace_points") = 5000U,
        py::arg("nominal_inductance_h") = 19.36e-3,
        py::arg("exact_simulink_reference") = true);

    py::class_<motor_core::StreamDecoder>(module, "V2StreamDecoder")
        .def(py::init<>())
        .def(
            "feed",
            [](motor_core::StreamDecoder& decoder, const py::bytes& value) {
                const std::string data = value;
                std::vector<motor_core::Frame> frames;
                {
                    py::gil_scoped_release release;
                    frames = decoder.feed(
                        reinterpret_cast<const std::uint8_t*>(data.data()),
                        data.size());
                }
                return frames_to_python(frames);
            },
            py::arg("chunk"))
        .def("reset", &motor_core::StreamDecoder::reset)
        .def_property_readonly("error_count",
                               &motor_core::StreamDecoder::error_count)
        .def_property_readonly("buffered_bytes",
                               &motor_core::StreamDecoder::buffered_bytes);

    py::class_<motor_core::TelemetryProcessor>(module, "TelemetryProcessor")
        .def(py::init<std::size_t, std::size_t, std::size_t>(),
             py::arg("max_f1_samples") = 131072,
             py::arg("max_bursts") = 4,
             py::arg("max_diagnostics") = 4096)
        .def(
            "ingest",
            [](motor_core::TelemetryProcessor& processor, std::uint8_t command,
               const py::bytes& value) {
                const std::string payload = value;
                py::gil_scoped_release release;
                return processor.ingest(
                    command,
                    reinterpret_cast<const std::uint8_t*>(payload.data()),
                    payload.size());
            },
            py::arg("command"), py::arg("payload"))
        .def("set_f1_rate_hz",
             &motor_core::TelemetryProcessor::set_f1_rate_hz)
        .def("set_rls_coefficients_si",
             &motor_core::TelemetryProcessor::set_rls_coefficients_si)
        .def("set_host_rls_enabled",
             &motor_core::TelemetryProcessor::set_host_rls_enabled,
             py::arg("enabled"), py::arg("reset") = true)
        .def_property_readonly("host_rls_enabled",
             &motor_core::TelemetryProcessor::host_rls_enabled)
        .def(
            "drain_f1",
            [](motor_core::TelemetryProcessor& processor,
               std::size_t max_samples) {
                std::vector<motor_core::F1Sample> samples;
                {
                    py::gil_scoped_release release;
                    samples = processor.drain_f1(max_samples);
                }
                return f1_samples_to_python(samples);
            },
            py::arg("max_samples") = 8192)
        .def(
            "drain_f1_columns",
            [](motor_core::TelemetryProcessor& processor,
               std::size_t max_samples) {
                std::vector<motor_core::F1Sample> samples;
                {
                    py::gil_scoped_release release;
                    samples = processor.drain_f1(max_samples);
                }
                return f1_samples_to_columns(samples);
            },
            py::arg("max_samples") = 8192)
        .def(
            "drain_f2",
            [](motor_core::TelemetryProcessor& processor,
               std::size_t max_samples) {
                std::vector<motor_core::F2Sample> samples;
                {
                    py::gil_scoped_release release;
                    samples = processor.drain_f2(max_samples);
                }
                return f2_samples_to_python(samples);
            },
            py::arg("max_samples") = 512)
        .def(
            "drain_f3",
            [](motor_core::TelemetryProcessor& processor,
               std::size_t max_samples) {
                std::vector<motor_core::F3Sample> samples;
                {
                    py::gil_scoped_release release;
                    samples = processor.drain_f3(max_samples);
                }
                return f3_samples_to_python(samples);
            },
            py::arg("max_samples") = 512)
        .def(
            "drain_bursts",
            [](motor_core::TelemetryProcessor& processor,
               std::size_t max_bursts) {
                std::vector<motor_core::BurstCapture> captures;
                {
                    py::gil_scoped_release release;
                    captures = processor.drain_bursts(max_bursts);
                }
                return bursts_to_python(captures);
            },
            py::arg("max_bursts") = 1)
        .def("stats", [](const motor_core::TelemetryProcessor& processor) {
            return telemetry_stats_to_python(processor.stats());
        })
        .def("reset", &motor_core::TelemetryProcessor::reset)
        .def("reset_burst", &motor_core::TelemetryProcessor::reset_burst);

    py::class_<motor_core::TcpV2Receiver>(module, "TcpV2Receiver")
        .def(py::init<std::size_t, std::size_t>(),
             py::arg("max_queue_frames") = 8192,
             py::arg("max_f1_samples") = 131072)
        .def(
            "start",
            [](motor_core::TcpV2Receiver& receiver, const std::string& host,
               std::uint16_t port, const std::string& local_host,
               double timeout_s) {
                py::gil_scoped_release release;
                receiver.start(host, port, local_host, timeout_s);
            },
            py::arg("host"), py::arg("port"), py::arg("local_host") = "",
            py::arg("timeout_s") = 2.0)
        .def(
            "stop",
            [](motor_core::TcpV2Receiver& receiver) {
                py::gil_scoped_release release;
                receiver.stop();
            })
        .def(
            "drain",
            [](motor_core::TcpV2Receiver& receiver, std::size_t max_frames) {
                std::vector<motor_core::Frame> frames;
                {
                    py::gil_scoped_release release;
                    frames = receiver.drain(max_frames);
                }
                return frames_to_python(frames);
            },
            py::arg("max_frames") = 512)
        .def(
            "drain_f1",
            [](motor_core::TcpV2Receiver& receiver,
               std::size_t max_samples) {
                std::vector<motor_core::F1Sample> samples;
                {
                    py::gil_scoped_release release;
                    samples = receiver.drain_f1(max_samples);
                }
                return f1_samples_to_python(samples);
            },
            py::arg("max_samples") = 8192)
        .def(
            "drain_f1_columns",
            [](motor_core::TcpV2Receiver& receiver,
               std::size_t max_samples) {
                std::vector<motor_core::F1Sample> samples;
                {
                    py::gil_scoped_release release;
                    samples = receiver.drain_f1(max_samples);
                }
                return f1_samples_to_columns(samples);
            },
            py::arg("max_samples") = 8192)
        .def(
            "drain_f2",
            [](motor_core::TcpV2Receiver& receiver,
               std::size_t max_samples) {
                std::vector<motor_core::F2Sample> samples;
                {
                    py::gil_scoped_release release;
                    samples = receiver.drain_f2(max_samples);
                }
                return f2_samples_to_python(samples);
            },
            py::arg("max_samples") = 512)
        .def(
            "drain_f3",
            [](motor_core::TcpV2Receiver& receiver,
               std::size_t max_samples) {
                std::vector<motor_core::F3Sample> samples;
                {
                    py::gil_scoped_release release;
                    samples = receiver.drain_f3(max_samples);
                }
                return f3_samples_to_python(samples);
            },
            py::arg("max_samples") = 512)
        .def(
            "drain_bursts",
            [](motor_core::TcpV2Receiver& receiver,
               std::size_t max_bursts) {
                std::vector<motor_core::BurstCapture> captures;
                {
                    py::gil_scoped_release release;
                    captures = receiver.drain_bursts(max_bursts);
                }
                return bursts_to_python(captures);
            },
            py::arg("max_bursts") = 1)
        .def("set_f1_rate_hz", &motor_core::TcpV2Receiver::set_f1_rate_hz)
        .def("set_rls_coefficients_si",
             &motor_core::TcpV2Receiver::set_rls_coefficients_si)
        .def("set_host_rls_enabled",
             &motor_core::TcpV2Receiver::set_host_rls_enabled,
             py::arg("enabled"), py::arg("reset") = true)
        .def_property_readonly("host_rls_enabled",
             &motor_core::TcpV2Receiver::host_rls_enabled)
        .def("set_telemetry_processing_enabled",
             &motor_core::TcpV2Receiver::set_telemetry_processing_enabled)
        .def("reset_burst", &motor_core::TcpV2Receiver::reset_burst)
        .def("stats", [](const motor_core::TcpV2Receiver& receiver) {
            const auto stats = receiver.stats();
            py::dict output;
            output["running"] = stats.running;
            output["rx_bytes"] = stats.rx_bytes;
            output["rx_frames"] = stats.rx_frames;
            output["dropped_frames"] = stats.dropped_frames;
            output["decoder_errors"] = stats.decoder_errors;
            output["queued_frames"] = stats.queued_frames;
            output["telemetry"] = telemetry_stats_to_python(stats.telemetry);
            output["last_error"] = stats.last_error;
            return output;
        });
}
