function buildRoot = build_host_model(hostPath, buildRoot)
%BUILD_HOST_MODEL Generate and compile the HOST model with R2024b.

arguments
    hostPath (1, 1) string
    buildRoot (1, 1) string
end

assert(strcmp(version('-release'), '2024b'), ...
    'build_host_model:WrongRelease', ...
    'This model must be built with MATLAB R2024b.');

[hostFolder, hostName] = fileparts(hostPath);
if ~isfolder(buildRoot)
    mkdir(buildRoot);
end

addpath(hostFolder);
pathCleanup = onCleanup(@() rmpath(hostFolder));
Simulink.fileGenControl('set', ...
    'CacheFolder', fullfile(buildRoot, 'cache'), ...
    'CodeGenFolder', fullfile(buildRoot, 'codegen'), ...
    'createDir', true);

open_system(hostPath);
modelCleanup = onCleanup(@() safe_close_model(hostName));
slbuild(hostName);
fprintf('HOST_BUILD_OK=1\n');
fprintf('HOST_BUILD_ROOT=%s\n', buildRoot);
close_system(hostName, 0);
clear modelCleanup pathCleanup;
end

function safe_close_model(modelName)
if bdIsLoaded(modelName)
    close_system(modelName, 0);
end
end
