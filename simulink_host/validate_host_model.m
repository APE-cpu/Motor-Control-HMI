function metrics = validate_host_model(sourcePath, hostPath)
%VALIDATE_HOST_MODEL Compare the HOST model against the original model.

arguments
    sourcePath (1, 1) string
    hostPath (1, 1) string
end

assert(strcmp(version('-release'), '2024b'), ...
    'validate_host_model:WrongRelease', ...
    'This validation must run with MATLAB R2024b.');

[sourceFolder, sourceName] = fileparts(sourcePath);
[hostFolder, hostName] = fileparts(hostPath);
addpath(sourceFolder, hostFolder);
pathCleanup = onCleanup(@() rmpath(sourceFolder, hostFolder));

in = Simulink.SimulationInput(sourceName);
in = in.setModelParameter('StopTime', '0.3');
sourceOut = sim(in);

sampleTime = 1e-5;
time = (0:sampleTime:0.3)';
speedRef = 500.0 * ones(size(time));
idRef = zeros(size(time));
loadTorque = 6.0 * ones(size(time));
loadTorque(time >= 0.15) = 10.0;

speedTs = timeseries(speedRef, time);
idTs = timeseries(idRef, time);
loadTs = timeseries(loadTorque, time);
speedTs.DataInfo.Interpolation = tsdata.interpolation('zoh');
idTs.DataInfo.Interpolation = tsdata.interpolation('zoh');
loadTs.DataInfo.Interpolation = tsdata.interpolation('zoh');

dataset = Simulink.SimulationData.Dataset;
dataset{1} = speedTs;
dataset{2} = idTs;
dataset{3} = loadTs;

in = Simulink.SimulationInput(hostName);
in = in.setModelParameter('StopTime', '0.3');
in = in.setExternalInput(dataset);
hostOut = sim(in);

metrics.iq = compare_timeseries(sourceOut.iq_log, hostOut.iq_log);
metrics.id = compare_timeseries(sourceOut.id_log, hostOut.id_log);
metrics.speed = compare_timeseries(sourceOut.n_log, hostOut.n_log);
metrics.torque = compare_timeseries(sourceOut.te_log, hostOut.te_log);

fprintf('VALIDATE_IQ_MAX_ABS=%.12g\n', metrics.iq.maxAbs);
fprintf('VALIDATE_ID_MAX_ABS=%.12g\n', metrics.id.maxAbs);
fprintf('VALIDATE_SPEED_MAX_ABS=%.12g\n', metrics.speed.maxAbs);
fprintf('VALIDATE_TORQUE_MAX_ABS=%.12g\n', metrics.torque.maxAbs);

assert(metrics.iq.maxAbs < 1e-6, ...
    'validate_host_model:IqMismatch', 'iq regression mismatch');
assert(metrics.id.maxAbs < 1e-6, ...
    'validate_host_model:IdMismatch', 'id regression mismatch');
assert(metrics.speed.maxAbs < 1e-6, ...
    'validate_host_model:SpeedMismatch', 'speed regression mismatch');
assert(metrics.torque.maxAbs < 1e-6, ...
    'validate_host_model:TorqueMismatch', 'torque regression mismatch');

fprintf('HOST_REGRESSION_OK=1\n');
clear pathCleanup;
end

function metric = compare_timeseries(reference, candidate)
referenceData = squeeze(reference.Data);
candidateData = interp1(candidate.Time, squeeze(candidate.Data), ...
    reference.Time, 'previous', 'extrap');
difference = referenceData - candidateData;
metric.maxAbs = max(abs(difference), [], 'all');
metric.rms = sqrt(mean(difference .^ 2, 'all'));
end
