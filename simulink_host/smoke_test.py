import argparse
import ctypes
import math
import time
from pathlib import Path


class Inputs(ctypes.Structure):
    _fields_ = [
        ("speed_ref_rpm", ctypes.c_double),
        ("id_ref_a", ctypes.c_double),
        ("load_torque_nm", ctypes.c_double),
    ]


class Parameters(ctypes.Structure):
    _fields_ = [
        ("speed_kp", ctypes.c_double),
        ("speed_ki", ctypes.c_double),
        ("iq_limit_a", ctypes.c_double),
        ("current_noise_variance", ctypes.c_double),
    ]


class Outputs(ctypes.Structure):
    _fields_ = [
        ("speed_rpm", ctypes.c_double),
        ("id_a", ctypes.c_double),
        ("iq_a", ctypes.c_double),
        ("id_ref_a", ctypes.c_double),
        ("iq_ref_a", ctypes.c_double),
        ("ud_v", ctypes.c_double),
        ("uq_v", ctypes.c_double),
        ("id_hat_a", ctypes.c_double),
        ("iq_hat_a", ctypes.c_double),
        ("theta_e_rad", ctypes.c_double),
        ("gate_abc", ctypes.c_double * 6),
        ("rls_param_d", ctypes.c_double * 7),
        ("rls_param_q", ctypes.c_double * 7),
        ("torque_nm", ctypes.c_double),
        ("simulation_time_s", ctypes.c_double),
    ]


def configure(dll: ctypes.CDLL) -> None:
    dll.pmsm_host_initialize.restype = ctypes.c_int
    dll.pmsm_host_terminate.restype = None
    dll.pmsm_host_set_inputs.argtypes = [ctypes.POINTER(Inputs)]
    dll.pmsm_host_set_inputs.restype = ctypes.c_int
    dll.pmsm_host_set_parameters.argtypes = [ctypes.POINTER(Parameters)]
    dll.pmsm_host_set_parameters.restype = ctypes.c_int
    dll.pmsm_host_get_parameters.argtypes = [ctypes.POINTER(Parameters)]
    dll.pmsm_host_get_parameters.restype = ctypes.c_int
    dll.pmsm_host_step.argtypes = [ctypes.c_uint32]
    dll.pmsm_host_step.restype = ctypes.c_int
    dll.pmsm_host_get_outputs.argtypes = [ctypes.POINTER(Outputs)]
    dll.pmsm_host_get_outputs.restype = ctypes.c_int
    dll.pmsm_host_error.restype = ctypes.c_char_p


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dll", type=Path)
    args = parser.parse_args()

    dll = ctypes.CDLL(str(args.dll.resolve()))
    configure(dll)

    assert dll.pmsm_host_initialize() == 0
    try:
        parameters = Parameters(0.8, 3.5, 5.0, 0.006)
        assert dll.pmsm_host_set_parameters(ctypes.byref(parameters)) == 0
        readback = Parameters()
        assert dll.pmsm_host_get_parameters(ctypes.byref(readback)) == 0
        assert (
            readback.speed_kp,
            readback.speed_ki,
            readback.iq_limit_a,
            readback.current_noise_variance,
        ) == (
            parameters.speed_kp,
            parameters.speed_ki,
            parameters.iq_limit_a,
            parameters.current_noise_variance,
        )

        inputs = Inputs(500.0, 0.0, 6.0)
        assert dll.pmsm_host_set_inputs(ctypes.byref(inputs)) == 0

        started = time.perf_counter()
        assert dll.pmsm_host_step(15_000) == 0
        inputs.load_torque_nm = 10.0
        assert dll.pmsm_host_set_inputs(ctypes.byref(inputs)) == 0
        assert dll.pmsm_host_step(15_000) == 0
        elapsed = time.perf_counter() - started

        outputs = Outputs()
        assert dll.pmsm_host_get_outputs(ctypes.byref(outputs)) == 0
        error = dll.pmsm_host_error()
        assert not error
        assert math.isclose(outputs.simulation_time_s, 0.3, abs_tol=1e-12)

        scalar_outputs = (
            outputs.speed_rpm,
            outputs.id_a,
            outputs.iq_a,
            outputs.iq_ref_a,
            outputs.ud_v,
            outputs.uq_v,
            outputs.torque_nm,
        )
        assert all(math.isfinite(value) for value in scalar_outputs)

        print("DLL_SMOKE_OK=1")
        print(f"SIMULATED_TIME_S={outputs.simulation_time_s:.12g}")
        print(f"WALL_TIME_MS={elapsed * 1000.0:.3f}")
        print(f"STEPS_PER_SECOND={30_000 / elapsed:.1f}")
        print(f"REALTIME_FACTOR={0.3 / elapsed:.3f}")
        print(f"FINAL_SPEED_RPM={outputs.speed_rpm:.9g}")
        print(f"FINAL_ID_A={outputs.id_a:.9g}")
        print(f"FINAL_IQ_A={outputs.iq_a:.9g}")
        print(f"FINAL_TORQUE_NM={outputs.torque_nm:.9g}")
    finally:
        dll.pmsm_host_terminate()


if __name__ == "__main__":
    main()
