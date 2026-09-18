function hostPath = prepare_host_model(sourcePath, hostPath)
%PREPARE_HOST_MODEL Create the R2024b host-integration copy of the model.
%   The source model is never modified. The generated HOST model exposes
%   command inputs, telemetry outputs, and a small set of runtime-tunable
%   parameters suitable for Simulink Coder integration.

arguments
    sourcePath (1, 1) string
    hostPath (1, 1) string
end

assert(strcmp(version('-release'), '2024b'), ...
    'prepare_host_model:WrongRelease', ...
    'This model must be prepared with MATLAB R2024b.');
assert(isfile(sourcePath), 'prepare_host_model:MissingSource', ...
    'Source model does not exist: %s', sourcePath);

[hostFolder, hostName] = fileparts(hostPath);
if ~isfolder(hostFolder)
    mkdir(hostFolder);
end

copyfile(sourcePath, hostPath, 'f');
open_system(hostPath);
cleanup = onCleanup(@() safe_close_model(hostName));

allBlocks = find_system(hostName, 'SearchDepth', 1, 'Type', 'Block');
positions = cell2mat(cellfun(@(block) get_param(block, 'Position'), ...
    allBlocks(2:end), 'UniformOutput', false));
leftX = min(positions(:, 1)) - 220;
rightX = max(positions(:, 3)) + 220;

replace_source_with_inport(hostName, '401', 'SpeedRef_rpm', 1, ...
    [leftX 80 leftX + 90 100]);
replace_source_with_inport(hostName, '24', 'IdRef_A', 2, ...
    [leftX 130 leftX + 90 150]);
replace_source_with_inport(hostName, '895', 'LoadTorque_Nm', 3, ...
    [leftX 180 leftX + 90 200]);

tagOutputs = {
    'n_r',        'Speed_rpm';
    'i_d',        'Id_A';
    'i_q',        'Iq_A';
    'id_ref',     'IdRef_A_Out';
    'iq_ref',     'IqRef_A';
    'u_d',        'Ud_V';
    'u_q',        'Uq_V';
    'id_hat',     'IdHat_A';
    'iq_hat',     'IqHat_A';
    'theta',      'ThetaE_rad';
    'gate',       'Gate_abc';
    'parameter_d','RlsParamD';
    'parameter_q','RlsParamQ'
};

for index = 1:size(tagOutputs, 1)
    y = 60 + (index - 1) * 45;
    add_tag_output(hostName, tagOutputs{index, 1}, ...
        tagOutputs{index, 2}, index, rightX, y);
end

torquePort = size(tagOutputs, 1) + 1;
torqueOut = add_block('built-in/Outport', ...
    hostName + "/Torque_Nm", ...
    'Port', string(torquePort), ...
    'Position', [rightX + 120 60 + (torquePort - 1) * 45 ...
                 rightX + 210 80 + (torquePort - 1) * 45]);
loggingBlock = Simulink.ID.getFullName(hostName + ":881");
loggingPorts = get_param(loggingBlock, 'PortHandles');
torqueLine = get_param(loggingPorts.Inport(1), 'Line');
torqueSource = get_param(torqueLine, 'SrcPortHandle');
torqueOutPorts = get_param(torqueOut, 'PortHandles');
add_line(hostName, torqueSource, torqueOutPorts.Inport(1), ...
    'autorouting', 'on');

modelWorkspace = get_param(hostName, 'ModelWorkspace');
assign_exported_parameter(modelWorkspace, 'HOST_SpeedKp', 0.8);
assign_exported_parameter(modelWorkspace, 'HOST_SpeedKi', 3.5);
assign_exported_parameter(modelWorkspace, 'HOST_IqLimit_A', 5.0);
assign_exported_parameter(modelWorkspace, ...
    'HOST_CurrentNoiseVariance', 0.006);

speedPid = Simulink.ID.getFullName(hostName + ":20");
set_param(speedPid, 'P', 'HOST_SpeedKp', 'I', 'HOST_SpeedKi');

iqLimit = Simulink.ID.getFullName(hostName + ":669");
set_param(iqLimit, 'UpperLimit', 'HOST_IqLimit_A', ...
    'LowerLimit', '-HOST_IqLimit_A');

noiseD = Simulink.ID.getFullName(hostName + ":897");
noiseQ = Simulink.ID.getFullName(hostName + ":899");
set_param(noiseD, 'Variance', 'HOST_CurrentNoiseVariance');
set_param(noiseQ, 'Variance', 'HOST_CurrentNoiseVariance');

set_param(hostName, ...
    'SolverType', 'Fixed-step', ...
    'Solver', 'FixedStepDiscrete', ...
    'FixedStep', '1e-5', ...
    'SystemTargetFile', 'grt.tlc', ...
    'TargetLang', 'C++', ...
    'CodeInterfacePackaging', 'Nonreusable function', ...
    'DefaultParameterBehavior', 'Tunable', ...
    'MatFileLogging', 'off', ...
    'GenerateReport', 'off');

set_param(hostName, 'SimulationCommand', 'update');
save_system(hostName, hostPath);
fprintf('HOST_MODEL=%s\n', hostPath);
fprintf('HOST_INPUT_COUNT=3\n');
fprintf('HOST_OUTPUT_COUNT=%d\n', torquePort);
fprintf('HOST_MODEL_DIRTY=%s\n', get_param(hostName, 'Dirty'));
close_system(hostName, 0);
clear cleanup;
end

function replace_source_with_inport(modelName, sourceSid, name, port, position)
sourceBlock = Simulink.ID.getFullName(modelName + ":" + sourceSid);
sourcePorts = get_param(sourceBlock, 'PortHandles');
sourceLine = get_param(sourcePorts.Outport(1), 'Line');
destinations = get_param(sourceLine, 'DstPortHandle');
delete_line(sourceLine);
delete_block(sourceBlock);

inport = add_block('built-in/Inport', modelName + "/" + name, ...
    'Port', string(port), 'Position', position);
inportPorts = get_param(inport, 'PortHandles');
for destination = destinations(:)'
    add_line(modelName, inportPorts.Outport(1), destination, ...
        'autorouting', 'on');
end
end

function add_tag_output(modelName, tag, name, port, rightX, y)
fromBlock = add_block('built-in/From', ...
    modelName + "/From_" + name, ...
    'GotoTag', tag, ...
    'Position', [rightX y rightX + 90 y + 20]);
outport = add_block('built-in/Outport', ...
    modelName + "/" + name, ...
    'Port', string(port), ...
    'Position', [rightX + 120 y rightX + 210 y + 20]);
fromPorts = get_param(fromBlock, 'PortHandles');
outPorts = get_param(outport, 'PortHandles');
add_line(modelName, fromPorts.Outport(1), outPorts.Inport(1), ...
    'autorouting', 'on');
end

function assign_exported_parameter(modelWorkspace, name, value)
parameter = Simulink.Parameter(value);
parameter.CoderInfo.StorageClass = 'ExportedGlobal';
assignin(modelWorkspace, name, parameter);
end

function safe_close_model(modelName)
if bdIsLoaded(modelName)
    close_system(modelName, 0);
end
end
