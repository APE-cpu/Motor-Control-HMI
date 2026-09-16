#include "motor_core/protocol_v2.hpp"
#include "motor_core/telemetry_processor.hpp"
#include "motor_core/tcp_v2_receiver.hpp"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <string>
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
    static PyObject* rate_hz_key = PyUnicode_InternFromString("rate_hz");
    static PyObject* angle_deg_key = PyUnicode_InternFromString("angle_deg");
    static PyObject* speed_rpm_key = PyUnicode_InternFromString("speed_rpm");
    static PyObject* iq_a_key = PyUnicode_InternFromString("iq_a");
    static PyObject* iqref_a_key = PyUnicode_InternFromString("iqref_a");
    static PyObject* ia_a_key = PyUnicode_InternFromString("ia_a");
    static PyObject* ib_a_key = PyUnicode_InternFromString("ib_a");
    static PyObject* vd_raw_key = PyUnicode_InternFromString("vd_raw");
    static PyObject* vq_raw_key = PyUnicode_InternFromString("vq_raw");
    static PyObject* vbus_v_key = PyUnicode_InternFromString("vbus_v");
    if (tick_ms_key == nullptr || rate_hz_key == nullptr ||
        angle_deg_key == nullptr || speed_rpm_key == nullptr ||
        iq_a_key == nullptr || iqref_a_key == nullptr || ia_a_key == nullptr ||
        ib_a_key == nullptr || vd_raw_key == nullptr || vq_raw_key == nullptr ||
        vbus_v_key == nullptr) {
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
        set_item(rate_hz_key, PyLong_FromUnsignedLong(sample.rate_hz));
        set_item(angle_deg_key, PyFloat_FromDouble(sample.angle_deg));
        set_item(speed_rpm_key, PyFloat_FromDouble(sample.speed_rpm));
        set_item(iq_a_key, PyFloat_FromDouble(sample.iq_a));
        set_item(iqref_a_key, PyFloat_FromDouble(sample.iqref_a));
        if (sample.has_phase_current) {
            set_item(ia_a_key, PyFloat_FromDouble(sample.ia_a));
            set_item(ib_a_key, PyFloat_FromDouble(sample.ib_a));
        }
        if (sample.has_voltage) {
            set_item(vd_raw_key, PyFloat_FromDouble(sample.vd_raw));
            set_item(vq_raw_key, PyFloat_FromDouble(sample.vq_raw));
            set_item(vbus_v_key, PyFloat_FromDouble(sample.vbus_v));
        }
        PyList_SET_ITEM(output.ptr(), static_cast<Py_ssize_t>(index), item);
    }
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
        output.append(std::move(item));
    }
    return output;
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

    module.def(
        "crc16_ccitt",
        [](const py::bytes& value, std::uint16_t initial) {
            const std::string data = value;
            return motor_core::crc16_ccitt(
                reinterpret_cast<const std::uint8_t*>(data.data()), data.size(),
                initial);
        },
        py::arg("data"), py::arg("initial") = 0xFFFF);

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
