# Native communication core

This directory contains the native communication data path: CRC-16/CCITT,
protocol-v2 streaming decode, a bounded-queue TCP telemetry receiver, F1/F2/F3
sample conversion, and F4 burst reassembly. The TCP socket, stream decoder, telemetry
parsing, and bounded buffers run in C++; the existing Python session state
machine and Qt signals consume converted data in batches.

Online ARX/RLS identification also runs here. Current firmware emits tagged
40-byte F1 samples. In addition to the continuous sequence, direct `Id/Iq`,
effective references, raw `Ia/Ib`, command `Vd/Vq`, bus ADC and measured VDDA,
F1/40 carries the final three TIM1 compare values and the angle used by the
inverse Park actuation. The host reconstructs the PWM-period-average alpha/beta
voltage and rotates it into dq. This is the actual digital actuation request
after voltage limiting, SVPWM and timer quantisation; it is not a terminal
voltage sensor and therefore does not include dead-time or semiconductor drops.
The parser still accepts tagged F1/32 and F1/30 plus untagged 12/16/22-byte
samples. Older formats fall back to the limited controller command and are
explicitly labelled as such.

Direct `Id` avoids reconstructing it with MCSDK's different
speed-compensated reverse-Park angle, while `Iq` remains the exact q-axis
feedback seen by the firmware PI (including its optional filter). The STM32
firmware does not execute estimator matrix operations in the 16 kHz
current-loop ISR.

The live and ordinary offline estimator uses a hardware-matched discrete ESO:
the capture sample interval, nominal `Ld=Lq=0.66 mH`, `wo=4000 rad/s`, a
one-sample actuation delay, and `B=Ts/L` (`0.0946969697 A/V` at 16 kHz).
The resulting `id_hat/iq_hat` feed the 7-regressor-per-axis ARX model:
`ihat(k-1..k-3)`, `ud/uq(k-2..k-3)`, `P0=1e6`, forgetting factor 1. No
coefficient clamp or physical projection is applied.

Exact R2024b replay remains an explicit test/reference option. In that mode
the generated constants for `MFPCC_DDM_coldstart_ESOrls.slx` are preserved at
100 kHz (`L=19.36 mH`, `B=0.0005165289256`). The source SLX plant is
`R=2.34 ohm`, `Ld=Lq=19.36 mH`, `flux=0.402 Wb`, whereas the current F407
target is `0.59 ohm`, `0.66 mH`, about `0.00585 Wb`. Exact replay must not be
presented as a hardware parameter result.

Real closed-loop physical validation is separate from the Simulink-reproduction
view. A bounded, default-off d/q PRBS reference probe uses different maximal
length polynomials on the two axes and makes the controller
reference an external instrument. Offline analysis forms `U/R` and `I/R`
cross-spectra, derives signed electrical speed from the same-cycle Park angle,
fits the actual 0..4 sample actuation delay at 1/8-cycle resolution, and reports physical
R/L only when full-data and split-data estimates agree. This avoids mistaking
the PI's anti-phase disturbance rejection voltage for motor impedance. For the
physical fit, raw `Ia/Ib` are transformed with the exact same-cycle Park angle;
the optional firmware feedback IIR therefore cannot masquerade as motor
inductance. A discontinuity in the 16-bit sample sequence rejects the dataset
instead of silently treating missing 16 kHz samples as adjacent measurements.

The legacy ARX(3) low-frequency-moment R/L fields remain available for API
compatibility, but every emitted sample marks them with
`rl_physical_validated=false`. An arbitrary two-input ARX(3) model does not
have a unique physical PMSM R/L interpretation. The UI therefore plots raw
`sum(a)` and own-axis `b0/b1` for the Simulink-reproduction path and reserves
physical R/L claims for the independent probe/IV result.

When the identification probe is turned off, the host freezes the injected
capture interval so ordinary post-probe operation cannot dilute the late Welch
segments or the early/late consistency check.  The physical estimate is also
compared with the firmware nominal `Rs=0.59 ohm, Ld/Lq=0.66 mH`; this only
changes the credibility verdict and never clamps, projects, or replaces the
reported estimate.

The frozen capture can be expanded and analyzed directly in a background
thread; CSV export is optional archival rather than a required intermediate
step. Disconnect also freezes a probe capture, preventing post-reconnect data
from contaminating the identification interval.

The hot F1 UI path uses a columnar batch (`drain_f1_columns`) so one Python
object carries whole arrays of angle, speed, current, and voltage values. The
legacy list-of-dictionaries API remains available for compatibility and tests.
When the vector page is visible and enabled, `drain_f1_columns` also hands the
decoded C++ F1 samples to a `VectorTrail` object before creating Python columns.
The native trail continuously selects at most 1000 points/s, computes the
current/flux coordinates, and retains at most 4000 points. The ~30 Hz UI refresh
receives numeric NumPy arrays rather than per-point Python objects. The Python
columns remain available to the monitor, CSV recorder, and other pages; the
simulation and compatibility paths still use their existing Python renderer.

Build and install into the active Python environment:

```powershell
python -m pip install .
```

For the source launcher, install a locally built copy beside the project
without replacing a `.pyd` held open by an existing HMI process:

```powershell
python -m pip install --no-deps --upgrade --target ..\native_core_runtime .
```

The source launcher prefers `native_core_runtime`; it falls back to the
environment's installed extension when that directory is absent.

The application selects `motor_core_cpp` automatically when it is importable.
The module exports `telemetry_schema_version=4`. The host requires schema 3 or
newer for F1/40; a schema-3 binary keeps the Python vector fallback, while
schema 4 exposes `VectorTrail` for the native point-cloud path.
Set `MOTOR_HMI_NATIVE_PROTOCOL=python` to force the Python decoder, or
`MOTOR_HMI_NATIVE_PROTOCOL=native` to fail fast when the native extension is
missing. `auto` is the default and safely falls back to Python.

The split RS-485 + Ethernet mode also selects the C++ TCP receiver by default.
Set `MOTOR_HMI_NATIVE_TRANSPORT=python` to force the original Python TCP path,
or `native` to disable automatic fallback when the extension is unavailable.

Set `MOTOR_HMI_NATIVE_TELEMETRY=python` to keep F1/F4 parsing in Python, or
`native` to require the C++ parser. `auto` is the default and falls back safely.
