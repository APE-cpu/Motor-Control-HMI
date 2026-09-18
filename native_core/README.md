# Native communication core

This directory contains the native communication data path: CRC-16/CCITT,
protocol-v2 streaming decode, a bounded-queue TCP telemetry receiver, F1/F2/F3
sample conversion, and F4 burst reassembly. The TCP socket, stream decoder, telemetry
parsing, and bounded buffers run in C++; the existing Python session state
machine and Qt signals consume converted data in batches.

Online ARX/RLS identification also runs here. It consumes the 22-byte F1
samples (`Ia/Ib`, electrical angle, `Vd/Vq`, and DC bus voltage), reconstructs
the MCSDK d/q currents with the firmware's exact Clarke/Park convention, and
publishes a 10 Hz result to the existing RLS monitor. The STM32 firmware does
not execute estimator matrix operations in the 16 kHz current-loop ISR.

The hot F1 UI path uses a columnar batch (`drain_f1_columns`) so one Python
object carries whole arrays of angle, speed, current, and voltage values. The
legacy list-of-dictionaries API remains available for compatibility and tests.

Build and install into the active Python environment:

```powershell
python -m pip install .
```

The application selects `motor_core_cpp` automatically when it is importable.
Set `MOTOR_HMI_NATIVE_PROTOCOL=python` to force the Python decoder, or
`MOTOR_HMI_NATIVE_PROTOCOL=native` to fail fast when the native extension is
missing. `auto` is the default and safely falls back to Python.

The split RS-485 + Ethernet mode also selects the C++ TCP receiver by default.
Set `MOTOR_HMI_NATIVE_TRANSPORT=python` to force the original Python TCP path,
or `native` to disable automatic fallback when the extension is unavailable.

Set `MOTOR_HMI_NATIVE_TELEMETRY=python` to keep F1/F4 parsing in Python, or
`native` to require the C++ parser. `auto` is the default and falls back safely.
