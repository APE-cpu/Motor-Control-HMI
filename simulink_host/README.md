# Simulink Coder host model

These scripts create and verify an upper-computer integration copy of
`MFPCC_DDM_coldstart_ESO.slx`. They must be run with MATLAB R2024b.

The source model is left unchanged. The generated `*_HOST.slx` model exposes:

- inputs: speed reference, d-axis current reference, and load torque;
- outputs: speed, torque, currents, voltage commands, observer estimates,
  electrical angle, gate commands, and RLS parameter vectors;
- exported parameters: speed-loop Kp/Ki, iq limit, and current-noise variance.

`prepare_host_model` creates the model copy, `validate_host_model` compares it
with the original model, and `build_host_model` runs Simulink Coder in an
isolated build directory.
