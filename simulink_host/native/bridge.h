#pragma once

#include <cstdint>

#ifdef _WIN32
#  ifdef PMSM_SIMULINK_HOST_EXPORTS
#    define PMSM_HOST_API extern "C" __declspec(dllexport)
#  else
#    define PMSM_HOST_API extern "C" __declspec(dllimport)
#  endif
#else
#  define PMSM_HOST_API extern "C"
#endif

struct PmsmHostInputs {
    double speed_ref_rpm;
    double id_ref_a;
    double load_torque_nm;
};

struct PmsmHostParameters {
    double speed_kp;
    double speed_ki;
    double iq_limit_a;
    double current_noise_variance;
};

struct PmsmHostOutputs {
    double speed_rpm;
    double id_a;
    double iq_a;
    double id_ref_a;
    double iq_ref_a;
    double ud_v;
    double uq_v;
    double id_hat_a;
    double iq_hat_a;
    double theta_e_rad;
    double gate_abc[6];
    double rls_param_d[7];
    double rls_param_q[7];
    double torque_nm;
    double simulation_time_s;
};

PMSM_HOST_API int pmsm_host_initialize();
PMSM_HOST_API void pmsm_host_terminate();
PMSM_HOST_API int pmsm_host_set_inputs(const PmsmHostInputs* inputs);
PMSM_HOST_API int pmsm_host_set_parameters(const PmsmHostParameters* parameters);
PMSM_HOST_API int pmsm_host_get_parameters(PmsmHostParameters* parameters);
PMSM_HOST_API int pmsm_host_step(std::uint32_t step_count);
PMSM_HOST_API int pmsm_host_get_outputs(PmsmHostOutputs* outputs);
PMSM_HOST_API const char* pmsm_host_error();
