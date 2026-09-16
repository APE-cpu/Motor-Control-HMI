#include "motor_core/protocol_v2.hpp"
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

    py::class_<motor_core::TcpV2Receiver>(module, "TcpV2Receiver")
        .def(py::init<std::size_t>(), py::arg("max_queue_frames") = 8192)
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
        .def("stats", [](const motor_core::TcpV2Receiver& receiver) {
            const auto stats = receiver.stats();
            py::dict output;
            output["running"] = stats.running;
            output["rx_bytes"] = stats.rx_bytes;
            output["rx_frames"] = stats.rx_frames;
            output["dropped_frames"] = stats.dropped_frames;
            output["decoder_errors"] = stats.decoder_errors;
            output["queued_frames"] = stats.queued_frames;
            output["last_error"] = stats.last_error;
            return output;
        });
}
