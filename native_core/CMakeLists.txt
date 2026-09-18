cmake_minimum_required(VERSION 3.20)
project(motor_core_cpp VERSION 0.5.0 LANGUAGES CXX)

find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
find_package(pybind11 CONFIG REQUIRED)

pybind11_add_module(motor_core_cpp
    src/bindings.cpp
    src/online_rls.cpp
    src/protocol_v2.cpp
    src/telemetry_processor.cpp
    src/tcp_v2_receiver.cpp
)
target_include_directories(motor_core_cpp PRIVATE include)
target_compile_features(motor_core_cpp PRIVATE cxx_std_17)
target_compile_definitions(motor_core_cpp PRIVATE
    MOTOR_CORE_VERSION="${PROJECT_VERSION}"
)

if(MSVC)
    target_compile_options(motor_core_cpp PRIVATE /W4 /permissive- /EHsc)
else()
    target_compile_options(motor_core_cpp PRIVATE -Wall -Wextra -Wpedantic)
endif()

if(WIN32)
    target_link_libraries(motor_core_cpp PRIVATE ws2_32)
endif()

install(TARGETS motor_core_cpp
    LIBRARY DESTINATION .
    RUNTIME DESTINATION .
)
