# Native communication core

This directory contains the first native slice of the communication stack:
CRC-16/CCITT and the protocol-v2 streaming frame decoder. The existing Python
transport, session state machine, and Qt signals remain unchanged.

Build and install into the active Python environment:

```powershell
python -m pip install .
```

The application selects `motor_core_cpp` automatically when it is importable.
Set `MOTOR_HMI_NATIVE_PROTOCOL=python` to force the Python decoder, or
`MOTOR_HMI_NATIVE_PROTOCOL=native` to fail fast when the native extension is
missing. `auto` is the default and safely falls back to Python.
