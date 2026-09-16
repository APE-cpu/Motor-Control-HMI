#include "motor_core/protocol_v2.hpp"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <string>
#include <vector>

namespace py = pybind11;

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
                py::list output;
                for (const auto& frame : frames) {
                    output.append(py::make_tuple(
                        frame.version, frame.address, frame.sequence,
                        frame.message_type, frame.command,
                        py::bytes(
                            reinterpret_cast<const char*>(frame.payload.data()),
                            frame.payload.size())));
                }
                return output;
            },
            py::arg("chunk"))
        .def("reset", &motor_core::StreamDecoder::reset)
        .def_property_readonly("error_count",
                               &motor_core::StreamDecoder::error_count)
        .def_property_readonly("buffered_bytes",
                               &motor_core::StreamDecoder::buffered_bytes);
}
