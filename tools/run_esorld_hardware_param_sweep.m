% Compare the original ESOrls model with the current F407 motor parameters.
% The source SLX is never saved or modified.  Every change is attached to a
% Simulink.SimulationInput and therefore exists only for that simulation run.

assert(strcmp(version('-release'), '2024b'), ...
    'This experiment must run with MATLAB/Simulink R2024b.');

modelDir = 'C:/Users/30276/Documents/Matlab Drive/MFPCC_work';
outputDir = 'C:/Users/30276/AI工作文件夹/上位机/artifacts/simulink_hardware_params';
if ~exist(outputDir, 'dir'), mkdir(outputDir); end
cd(modelDir);

mdl = 'MFPCC_DDM_coldstart_ESOrls';
Tstop = 0.3;
Ts = 1e-5;
Rhw = 0.59;
Lhw = 0.66e-3;
fluxHw = 0.00585;

% A baseline simulation loads the model through SimulationInput/sim.  Stable
% Simulink IDs are then resolved to block paths; no hand-typed block names.
in = Simulink.SimulationInput(mdl);
in = in.setModelParameter('StopTime', num2str(Tstop));
out = sim(in);

machineBlock = Simulink.ID.getFullName([mdl ':2']);
torqueToCurrentBlock = Simulink.ID.getFullName([mdl ':820']);
esoQBlock = Simulink.ID.getFullName([mdl ':757']);
esoDBlock = Simulink.ID.getFullName([mdl ':765']);

cases = collectCase(out, '原始对象', 2.34, 19.36e-3, 0.402, Ts);

% Case 2: only the physical PMSM block changes.  Controller, ESO and RLS are
% deliberately left untouched to expose the model mismatch.
in = Simulink.SimulationInput(mdl);
in = in.setModelParameter('StopTime', num2str(Tstop));
in = applyMachine(in, machineBlock, Rhw, Lhw, fluxHw);
out = sim(in);
cases(2) = collectCase(out, '真机对象/原ESO', Rhw, Lhw, fluxHw, Ts);

% Case 3: preserve the RLS itself but let the ESO use the true Ts/L input
% coefficient.  wo, delay, RLS P0/lambda and all seven regressors are intact.
in = Simulink.SimulationInput(mdl);
in = in.setModelParameter('StopTime', num2str(Tstop));
in = applyMachine(in, machineBlock, Rhw, Lhw, fluxHw);
in = applyEsoInputGain(in, esoDBlock, esoQBlock, Ts, Lhw);
out = sim(in);
cases(3) = collectCase(out, '真机对象/匹配ESO', Rhw, Lhw, fluxHw, Ts);

% Case 4: also update the known torque-to-iq conversion flux.  This is the
% internally consistent real-parameter simulation; the RLS remains unchanged.
in = Simulink.SimulationInput(mdl);
in = in.setModelParameter('StopTime', num2str(Tstop));
in = applyMachine(in, machineBlock, Rhw, Lhw, fluxHw);
in = applyEsoInputGain(in, esoDBlock, esoQBlock, Ts, Lhw);
in = in.setBlockParameter( ...
    torqueToCurrentBlock, 'Gain', sprintf('1/1.5/4/%.17g', fluxHw));
out = sim(in);
cases(4) = collectCase(out, '真机对象/匹配ESO与磁链', Rhw, Lhw, fluxHw, Ts);

labels = string({cases.label})';
summary = table(labels, [cases.iqMeasuredError]', [cases.iqEstimatedError]', ...
    [cases.idMeasuredRms]', [cases.idEstimatedRms]', ...
    [cases.sigmaAd]', [cases.sigmaAq]', [cases.bOwnD]', [cases.bOwnQ]', ...
    [cases.rEqD]', [cases.rEqQ]', [cases.lEqD_mH]', [cases.lEqQ_mH]', ...
    'VariableNames', {'case_name','iq_measured_rms_A','iq_eso_rms_A', ...
    'id_measured_rms_A','id_eso_rms_A','sum_a_d','sum_a_q', ...
    'b_own_d_A_per_V','b_own_q_A_per_V','R_eq_d_ohm','R_eq_q_ohm', ...
    'L_eq_d_mH','L_eq_q_mH'});
writetable(summary, fullfile(outputDir, 'summary.csv'), 'Encoding', 'UTF-8');

thetaNames = [compose('theta_d_%d', 1:7), compose('theta_q_%d', 1:7)];
thetaMatrix = zeros(numel(cases), 14);
for k = 1:numel(cases)
    thetaMatrix(k,:) = [cases(k).thetaDMean(:).', cases(k).thetaQMean(:).'];
end
thetaSummary = array2table(thetaMatrix, 'VariableNames', thetaNames);
thetaSummary = addvars(thetaSummary, labels, 'Before', 1, ...
    'NewVariableNames', 'case_name');
writetable(thetaSummary, fullfile(outputDir, 'theta_7x2.csv'), ...
    'Encoding', 'UTF-8');

fig = figure('Visible','off','Color','w','Position',[40 40 1800 1050]);
tiledlayout(3,4,'TileSpacing','compact','Padding','compact');
for k = 1:numel(cases)
    c = cases(k);
    nexttile(k); hold on; grid on;
    plot(c.tq*1e3, c.iq, 'Color',[0.72 0.78 0.88], 'LineWidth',0.4);
    plot(c.tq*1e3, c.iqhat, 'r', 'LineWidth',1.0);
    plot(c.tq*1e3, c.iqref, 'k--', 'LineWidth',0.9);
    title(c.label); xlabel('t / ms'); ylabel('i_q / A');
    if k == 1, legend('测量','ESO','给定','Location','best'); end

    nexttile(4+k); hold on; grid on;
    plot(c.tpd*1e3, sum(c.thetaD(1:3,:),1), 'Color',[0.10 0.55 0.90]);
    plot(c.tpq*1e3, sum(c.thetaQ(1:3,:),1), 'Color',[0.72 0.25 0.72]);
    yline(1-Rhw*Ts/Lhw, 'k:', '真机一阶a');
    xlabel('t / ms'); ylabel('\Sigmaa');
    if k == 1, legend('\Sigmaa_d','\Sigmaa_q','Location','best'); end

    nexttile(8+k); hold on; grid on;
    plot(c.tpd*1e3, c.thetaD(4,:), 'Color',[0.10 0.65 0.55]);
    plot(c.tpq*1e3, c.thetaQ(6,:), 'Color',[0.95 0.40 0.22]);
    yline(Ts/Lhw, 'k:', '真机Ts/L');
    xlabel('t / ms'); ylabel('本轴b / A/V');
    if k == 1, legend('b_d','b_q','Location','best'); end
end
sgtitle('R2024b ESOrls：原对象与真机电气参数对照');
exportgraphics(fig, fullfile(outputDir, 'comparison.png'), 'Resolution',160);
close(fig);

save(fullfile(outputDir, 'sweep_results.mat'), 'cases', 'summary', ...
    'thetaSummary', 'Rhw', 'Lhw', 'fluxHw', 'Ts', 'Tstop');

disp(summary);
disp(thetaSummary);
fprintf('Artifacts: %s\n', outputDir);

function in = applyMachine(in, blockPath, resistance, inductance, flux)
in = in.setBlockParameter( ...
    blockPath, 'Resistance', sprintf('%.17g', resistance));
in = in.setBlockParameter( ...
    blockPath, 'dqInductances', ...
    sprintf('[%.17g %.17g]', inductance, inductance));
in = in.setBlockParameter( ...
    blockPath, 'Flux', sprintf('%.17g', flux));
end

function in = applyEsoInputGain(in, dBlock, qBlock, sampleTime, inductance)
sysExpression = sprintf('ss(1,%.17g,1,0,%.17g)', ...
    sampleTime/inductance, sampleTime);
in = in.setBlockParameter(dBlock, 'sys', sysExpression);
in = in.setBlockParameter(qBlock, 'sys', sysExpression);
end

function result = collectCase(out, label, resistance, inductance, flux, Ts)
[tq, iq] = unpackLog(out.iq_log);
[tr, iqref] = unpackLog(out.iqref_log);
[td, id] = unpackLog(out.id_log);
[tqh, iqhat] = unpackLog(out.iqhat_log);
[tdh, idhat] = unpackLog(out.idhat_log);
[tpd, thetaD] = unpackLog(out.theta_d_log);
[tpq, thetaQ] = unpackLog(out.theta_q_log);
if size(thetaD,1) ~= 7 && size(thetaD,2) == 7, thetaD = thetaD.'; end
if size(thetaQ,1) ~= 7 && size(thetaQ,2) == 7, thetaQ = thetaQ.'; end

iqref = interp1(tr, iqref, tq, 'linear', 'extrap');
iqhat = interp1(tqh, iqhat, tq, 'linear', 'extrap');
idhat = interp1(tdh, idhat, td, 'linear', 'extrap');
tailQ = tq >= tq(end)-0.02;
tailD = td >= td(end)-0.02;
tailPD = tpd >= tpd(end)-0.02;
tailPQ = tpq >= tpq(end)-0.02;
thetaDMean = mean(thetaD(:,tailPD),2);
thetaQMean = mean(thetaQ(:,tailPQ),2);
[rEqD,lEqD] = projectRl(thetaDMean, 4, Ts);
[rEqQ,lEqQ] = projectRl(thetaQMean, 6, Ts);

result = struct( ...
    'label',label,'resistance',resistance,'inductance',inductance, ...
    'flux',flux,'tq',tq,'iq',iq,'iqref',iqref,'iqhat',iqhat, ...
    'td',td,'id',id,'idhat',idhat,'tpd',tpd,'thetaD',thetaD, ...
    'tpq',tpq,'thetaQ',thetaQ,'thetaDMean',thetaDMean, ...
    'thetaQMean',thetaQMean, ...
    'iqMeasuredError',rms(iq(tailQ)-iqref(tailQ)), ...
    'iqEstimatedError',rms(iqhat(tailQ)-iqref(tailQ)), ...
    'idMeasuredRms',rms(id(tailD)), ...
    'idEstimatedRms',rms(idhat(tailD)), ...
    'sigmaAd',sum(thetaDMean(1:3)), ...
    'sigmaAq',sum(thetaQMean(1:3)), ...
    'bOwnD',thetaDMean(4),'bOwnQ',thetaQMean(6), ...
    'rEqD',rEqD,'rEqQ',rEqQ, ...
    'lEqD_mH',1e3*lEqD,'lEqQ_mH',1e3*lEqQ);
end

function [resistance,inductance] = projectRl(theta, ownIndex, Ts)
a = theta(1:3);
b0 = theta(ownIndex);
b1 = theta(ownIndex+1);
aDc = 1-sum(a);
bDc = b0+b1;
resistance = aDc/bDc;
moment = (b0+2*b1)/bDc + (a(1)+2*a(2)+3*a(3))/aDc;
inductance = resistance*Ts*moment;
end

function [t,v] = unpackLog(x)
if isa(x,'timeseries')
    t = x.Time;
    v = squeeze(x.Data);
else
    t = x.time;
    v = squeeze(x.signals.values);
end
t = t(:);
if isvector(v), v = v(:); end
end
