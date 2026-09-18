cmake_minimum_required(VERSION 3.20)

project(pmsm_simulink_host LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(MATLAB_ROOT "C:/Program Files/MATLAB/R2024b" CACHE PATH
    "MATLAB R2024b installation directory")
set(GENERATED_DIR "" CACHE PATH
    "Directory containing Simulink Coder generated source files")

if(NOT EXISTS "${GENERATED_DIR}/MFPCC_DDM_coldstart_ESO_HOST.cpp")
    message(FATAL_ERROR
        "GENERATED_DIR must point to MFPCC_DDM_coldstart_ESO_HOST_grt_rtw")
endif()

get_filename_component(GENERATED_ROOT "${GENERATED_DIR}" DIRECTORY)

add_library(pmsm_simulink_host SHARED
    bridge.cpp
    "${GENERATED_DIR}/MFPCC_DDM_coldstart_ESO_HOST.cpp"
    "${GENERATED_DIR}/MFPCC_DDM_coldstart_ESO_HOST_data.cpp"
    "${GENERATED_DIR}/rtGetInf.cpp"
    "${GENERATED_DIR}/rtGetNaN.cpp"
    "${GENERATED_DIR}/rt_nonfinite.cpp"
)

target_compile_definitions(pmsm_simulink_host PRIVATE
    PMSM_SIMULINK_HOST_EXPORTS
    CLASSIC_INTERFACE=0
    ALLOCATIONFCN=0
    MAT_FILE=0
    ONESTEPFCN=1
    TERMFCN=1
    MULTI_INSTANCE_CODE=0
    INTEGER_CODE=0
    MT=0
    TID01EQ=0
    MODEL=MFPCC_DDM_coldstart_ESO_HOST
    NUMST=1
    NCSTATES=0
    HAVESTDIO
    RT
    USE_RTMODEL
    _CRT_SECURE_NO_WARNINGS
)

target_include_directories(pmsm_simulink_host PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}"
    "${GENERATED_ROOT}"
    "${GENERATED_DIR}"
    "${MATLAB_ROOT}/extern/include"
    "${MATLAB_ROOT}/simulink/include"
    "${MATLAB_ROOT}/rtw/c/src"
    "${MATLAB_ROOT}/rtw/c/src/ext_mode/common"
    "${MATLAB_ROOT}/toolbox/physmod/powersys/facts/facts"
    "${MATLAB_ROOT}/toolbox/physmod/powersys/DR/DR"
)

if(MSVC)
    set_property(TARGET pmsm_simulink_host PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    target_compile_options(pmsm_simulink_host PRIVATE
        /W4 /wd4251 /Zc:__cplusplus)
endif()

target_link_libraries(pmsm_simulink_host PRIVATE
    kernel32
    ws2_32
    mswsock
    advapi32
)
