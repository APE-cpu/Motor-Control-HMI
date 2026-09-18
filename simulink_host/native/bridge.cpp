#include "bridge.h"

#include "MFPCC_DDM_coldstart_ESO_HOST.h"

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace {

constexpr double kStepSeconds = 1.0e-5;
bool initialized = false;
std::uint64_t completedSteps = 0;

bool finite(double value)
{
    return std::isfinite(value);
}

}  // namespace

int pmsm_host_initialize()
{
    if (initialized) {
        MFPCC_DDM_coldstart_ESO_HOST_terminate();
    }
    MFPCC_DDM_coldstart_ESO_HOST_initialize();
    completedSteps = 0;
    initialized = true;
    return rtmGetErrorStatus(MFPCC_DDM_coldstart_ESO_HOST_M) == nullptr ? 0 : 1;
}

void pmsm_host_terminate()
{
    if (initialized) {
        MFPCC_DDM_coldstart_ESO_HOST_terminate();
        initialized = false;
        completedSteps = 0;
    }
}

int pmsm_host_set_inputs(const PmsmHostInputs* inputs)
{
    if (inputs == nullptr || !finite(inputs->speed_ref_rpm) ||
        !finite(inputs->id_ref_a) || !finite(inputs->load_torque_nm)) {
        return -1;
    }

    MFPCC_DDM_coldstart_ESO_HOST_U.SpeedRef_rpm = inputs->speed_ref_rpm;
    MFPCC_DDM_coldstart_ESO_HOST_U.IdRef_A = inputs->id_ref_a;
    MFPCC_DDM_coldstart_ESO_HOST_U.LoadTorque_Nm = inputs->load_torque_nm;
    return 0;
}

int pmsm_host_set_parameters(const PmsmHostParameters* parameters)
{
    if (parameters == nullptr || !finite(parameters->speed_kp) ||
        !finite(parameters->speed_ki) || !finite(parameters->iq_limit_a) ||
        !finite(parameters->current_noise_variance) ||
        parameters->iq_limit_a <= 0.0 ||
        parameters->current_noise_variance < 0.0) {
        return -1;
    }

    HOST_SpeedKp = parameters->speed_kp;
    HOST_SpeedKi = parameters->speed_ki;
    HOST_IqLimit_A = parameters->iq_limit_a;
    HOST_CurrentNoiseVariance = parameters->current_noise_variance;
    return 0;
}

int pmsm_host_get_parameters(PmsmHostParameters* parameters)
{
    if (parameters == nullptr) {
        return -1;
    }
    parameters->speed_kp = HOST_SpeedKp;
    parameters->speed_ki = HOST_SpeedKi;
    parameters->iq_limit_a = HOST_IqLimit_A;
    parameters->current_noise_variance = HOST_CurrentNoiseVariance;
    return 0;
}

int pmsm_host_step(std::uint32_t step_count)
{
    if (!initialized) {
        return -1;
    }

    for (std::uint32_t index = 0; index < step_count; ++index) {
        MFPCC_DDM_coldstart_ESO_HOST_step();
        ++completedSteps;
        if (rtmGetErrorStatus(MFPCC_DDM_coldstart_ESO_HOST_M) != nullptr) {
            return 1;
        }
    }
    return 0;
}

int pmsm_host_get_outputs(PmsmHostOutputs* outputs)
{
    if (!initialized || outputs == nullptr) {
        return -1;
    }

    const auto& model = MFPCC_DDM_coldstart_ESO_HOST_Y;
    outputs->speed_rpm = model.Speed_rpm;
    outputs->id_a = model.Id_A;
    outputs->iq_a = model.Iq_A;
    outputs->id_ref_a = model.IdRef_A_Out;
    outputs->iq_ref_a = model.IqRef_A;
    outputs->ud_v = model.Ud_V;
    outputs->uq_v = model.Uq_V;
    outputs->id_hat_a = model.IdHat_A;
    outputs->iq_hat_a = model.IqHat_A;
    outputs->theta_e_rad = model.ThetaE_rad;
    std::copy_n(model.Gate_abc, 6, outputs->gate_abc);
    std::copy_n(model.RlsParamD, 7, outputs->rls_param_d);
    std::copy_n(model.RlsParamQ, 7, outputs->rls_param_q);
    outputs->torque_nm = model.Torque_Nm;
    outputs->simulation_time_s = completedSteps * kStepSeconds;
    return 0;
}

const char* pmsm_host_error()
{
    return initialized ? rtmGetErrorStatus(MFPCC_DDM_coldstart_ESO_HOST_M)
                       : "model is not initialized";
}
