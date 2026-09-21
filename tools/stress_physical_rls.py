"""Reproducible hardware-like stress test for physical dq identification.

This is deliberately stricter than the unit-test fixture: the controller sees
quantized noisy phase-current feedback, the motor has dq cross coupling and
back EMF, the commanded voltage is delayed/quantized, and Vbus contains ripple.
It never alters or constrains estimator results.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

import numpy as np


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rls_offline import identify_physical_dq_iv


def _prbs(count: int, amplitude_a: float, seed: int, *, q_axis: bool,
          offset_a: float) -> np.ndarray:
    values = np.empty(count, dtype=float)
    state = seed
    divider = 0
    for index in range(count):
        divider += 1
        if divider >= 8:
            divider = 0
            if q_axis:
                bit = ((state >> 0) ^ (state >> 2) ^
                       (state >> 3) ^ (state >> 5)) & 1
                state = ((state >> 1) | (bit << 15)) & 0xFFFF
            else:
                bit = ((state >> 14) ^ (state >> 13)) & 1
                state = ((state << 1) | bit) & 0x7FFF
            state = state or 1
        values[index] = offset_a + (amplitude_a if state & 1 else -amplitude_a)
    return values


def simulate(*, duration_s: float, amplitude_a: float, noise_std_a: float,
             seed: int, colored_noise: bool = True) -> dict[str, object]:
    fs = 16_000
    count = round(duration_s * fs)
    ts = 1.0 / fs
    resistance = 0.59
    inductance = 0.00066
    flux_wb = 0.00585
    speed_rpm = 500.0
    pole_pairs = 4
    omega_e = speed_rpm * pole_pairs * 2.0 * math.pi / 60.0
    kp = 1.524
    ki_step = 1362.7 / fs
    vdda_v = 3.15
    current_per_digit = vdda_v / (65536.0 * 0.01000 * 8.00)
    rng = np.random.default_rng(seed)

    id_ref = _prbs(count, amplitude_a, 0x0001, q_axis=False, offset_a=0.0)
    iq_ref = _prbs(count, amplitude_a, 0x2345, q_axis=True, offset_a=1.2)
    actual_d = np.zeros(count)
    actual_q = np.zeros(count)
    measured_d = np.zeros(count)
    measured_q = np.zeros(count)
    phase_a = np.zeros(count)
    phase_b = np.zeros(count)
    ud_command = np.zeros(count)
    uq_command = np.zeros(count)
    integral_d = integral_q = 0.0
    delayed_d = [0.0, 0.0]
    delayed_q = [0.0, 0.0]

    time_s = np.arange(count, dtype=float) / fs

    def phase_noise(phase: float) -> np.ndarray:
        if noise_std_a <= 0.0:
            return np.zeros(count)
        if colored_noise:
            # Old hardware capture has strong components around the electrical
            # fundamental/third harmonic and the 638/688 Hz current-loop
            # region.  Preserve those narrow-band disturbances instead of
            # validating only against friendly white noise.
            noise = 0.55 * rng.normal(size=count)
            noise += 0.55 * np.sin(2.0 * math.pi * 638.0 * time_s + phase)
            noise += 0.50 * np.sin(2.0 * math.pi * 688.0 * time_s + 0.7 + phase)
            noise += 0.30 * np.sin(2.0 * math.pi * 99.0 * time_s + 1.3 + phase)
            noise += 0.20 * np.sin(2.0 * math.pi * 32.0 * time_s + 0.2 + phase)
        else:
            noise = rng.normal(size=count)
        noise -= np.mean(noise)
        return noise * (noise_std_a / max(float(np.std(noise)), 1e-15))

    phase_a_noise = phase_noise(0.0)
    phase_b_noise = phase_noise(2.0 * math.pi / 3.0)

    # Exact ZOH discretisation of the continuous PMSM dq electrical model.
    # A forward-Euler fixture would itself turn 0.660 mH into about 0.641 mH
    # at 16 kHz and would therefore bake a numerical bias into this test.
    decay = resistance / inductance
    decay_step = math.exp(-decay * ts)
    rotation = omega_e * ts
    state_matrix = decay_step * np.array((
        (math.cos(rotation), math.sin(rotation)),
        (-math.sin(rotation), math.cos(rotation)),
    ))
    continuous_matrix = np.array(((-decay, omega_e),
                                  (-omega_e, -decay)))
    input_integral = np.linalg.solve(
        continuous_matrix, state_matrix - np.eye(2))

    for index in range(1, count):
        applied_d = delayed_d.pop(0)
        applied_q = delayed_q.pop(0)
        state = state_matrix @ np.array((actual_d[index - 1],
                                         actual_q[index - 1]))
        forcing = np.array((applied_d / inductance,
                            applied_q / inductance -
                            omega_e * flux_wb / inductance))
        state += input_integral @ forcing
        actual_d[index], actual_q[index] = state

        angle = index * omega_e / fs
        alpha = (actual_q[index] * math.cos(angle) +
                 actual_d[index] * math.sin(angle))
        beta = (-actual_q[index] * math.sin(angle) +
                actual_d[index] * math.cos(angle))
        ia = alpha + phase_a_noise[index]
        ib = ((-math.sqrt(3.0) * beta - alpha) / 2.0 +
              phase_b_noise[index])
        ia = np.rint(ia / current_per_digit) * current_per_digit
        ib = np.rint(ib / current_per_digit) * current_per_digit
        phase_a[index] = ia
        phase_b[index] = ib
        measured_beta = -(ia + 2.0 * ib) / math.sqrt(3.0)
        measured_d[index] = (ia * math.sin(angle) +
                             measured_beta * math.cos(angle))
        measured_q[index] = (ia * math.cos(angle) -
                             measured_beta * math.sin(angle))

        error_d = id_ref[index] - measured_d[index]
        error_q = iq_ref[index] - measured_q[index]
        integral_d += ki_step * error_d
        integral_q += ki_step * error_q
        ud_command[index] = kp * error_d + integral_d
        uq_command[index] = kp * error_q + integral_q
        vbus = 24.0 + 0.4 * math.sin(2.0 * math.pi * 100.0 * index / fs)
        volts_per_digit = vbus / (math.sqrt(3.0) * 32768.0)
        delayed_d.append(np.rint(ud_command[index] / volts_per_digit) *
                         volts_per_digit)
        delayed_q.append(np.rint(uq_command[index] / volts_per_digit) *
                         volts_per_digit)

    angle = np.arange(count, dtype=float) * omega_e / fs
    angle_raw = np.rint((angle % (2.0 * math.pi)) /
                        (2.0 * math.pi) * 65536.0) % 65536.0
    angle_deg = angle_raw * (360.0 / 65536.0)
    vbus_v = 24.0 + 0.4 * np.sin(
        2.0 * math.pi * 100.0 * np.arange(count) / fs)
    volts_per_digit = vbus_v / (math.sqrt(3.0) * 32768.0)
    columns = {
        "count": count,
        "rate_hz": fs,
        "tick_ms": np.arange(count, dtype=np.uint32) // 16,
        "sample_seq": np.arange(count, dtype=np.uint32) & 0xFFFF,
        "sequence_source_direct": True,
        "angle_deg": angle_deg,
        "speed_rpm": np.full(count, speed_rpm),
        "id_a": measured_d,
        "iq_a": measured_q,
        "id_source_direct": True,
        "idref_a": np.rint(id_ref / current_per_digit) * current_per_digit,
        "iqref_a": np.rint(iq_ref / current_per_digit) * current_per_digit,
        "ia_a": phase_a,
        "ib_a": phase_b,
        "vd_raw": np.rint(ud_command / volts_per_digit),
        "vq_raw": np.rint(uq_command / volts_per_digit),
        "vbus_v": vbus_v,
        "vdda_v": np.full(count, vdda_v),
        "vdda_source_direct": True,
    }
    result = identify_physical_dq_iv(columns)
    return {
        "seed": seed,
        "duration_s": duration_s,
        "amplitude_a": amplitude_a,
        "noise_std_a": noise_std_a,
        "noise_profile": "hardware_colored" if colored_noise else "white",
        "verdict": result.get("verdict"),
        "nominal_match": result.get("nominal_match"),
        "rd_ohm": result.get("rd_ohm"),
        "rq_ohm": result.get("rq_ohm"),
        "ld_mh": result.get("ld_mh"),
        "lq_mh": result.get("lq_mh"),
        "d_coherence": result.get("d_axis", {}).get("median_coherence"),
        "q_coherence": result.get("q_axis", {}).get("median_coherence"),
        "d_fit_nrmse": result.get("d_axis", {}).get("fit_nrmse"),
        "q_fit_nrmse": result.get("q_axis", {}).get("fit_nrmse"),
        "reasons": result.get("reasons"),
        "nominal_reasons": result.get("nominal_reasons"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--amplitude", type=float, default=0.08)
    parser.add_argument("--noise", type=float, default=0.20)
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument(
        "--white-noise", action="store_true",
        help="use white noise instead of the hardware-like colored profile")
    args = parser.parse_args()
    reports = [simulate(duration_s=args.duration, amplitude_a=args.amplitude,
                        noise_std_a=args.noise, seed=seed,
                        colored_noise=not args.white_noise)
               for seed in range(args.seeds)]
    print(json.dumps(reports, ensure_ascii=False, separators=(",", ":")))
    return 0 if all(item["nominal_match"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
