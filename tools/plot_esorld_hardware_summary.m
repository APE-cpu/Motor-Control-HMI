% Render a compact interpretation of run_esorld_hardware_param_sweep.m.
assert(strcmp(version('-release'), '2024b'), ...
    'This plot must run with MATLAB R2024b.');

outputDir = 'C:/Users/30276/AI工作文件夹/上位机/artifacts/simulink_hardware_params';
load(fullfile(outputDir, 'sweep_results.mat'), 'cases', 'Ts');

labels = string({cases.label});
trueL = 1e3*[cases.inductance];
lFromBd = 1e3*Ts./[cases.bOwnD];
lFromBq = 1e3*Ts./[cases.bOwnQ];
errD = 100*(lFromBd-trueL)./trueL;
errQ = 100*(lFromBq-trueL)./trueL;

fig = figure('Visible','off','Color','w','Position',[60 60 1550 900]);
tiledlayout(2,2,'TileSpacing','compact','Padding','compact');

nexttile; hold on; grid on;
bar([lFromBd(:),lFromBq(:)]);
plot(1:numel(cases),trueL,'ko--','LineWidth',1.3,'MarkerFaceColor','w');
set(gca,'YScale','log','XTick',1:numel(cases),'XTickLabel',labels);
xtickangle(12); ylabel('电感 / mH（对数轴）');
title('由本轴b_0反推 L=T_s/b_0');
legend('Ld估计','Lq估计','模型真值','Location','northwest');

nexttile; hold on; grid on;
bar([errD(:),errQ(:)]);
yline(0,'k-');
set(gca,'XTick',1:numel(cases),'XTickLabel',labels);
xtickangle(12); ylabel('相对误差 / %');
title('电感辨识误差');
legend('d轴','q轴','Location','northwest');

nexttile; hold on; grid on;
bar([[cases.iqMeasuredError].',[cases.iqEstimatedError].', ...
     [cases.idMeasuredRms].',[cases.idEstimatedRms].']);
set(gca,'XTick',1:numel(cases),'XTickLabel',labels);
xtickangle(12); ylabel('RMS / A');
title('控制电流与ESO误差');
legend('Iq测量跟踪','Iq ESO跟踪','Id测量','Id ESO','Location','northwest');

nexttile; axis off;
text(0,0.92,'主要结论','FontSize',16,'FontWeight','bold');
text(0,0.78,sprintf('原对象: Ld/Lq = %.4f / %.4f mH', ...
    lFromBd(1),lFromBq(1)),'FontSize',13);
text(0,0.64,sprintf('真机对象 + 原ESO: %.4f / %.4f mH', ...
    lFromBd(2),lFromBq(2)),'FontSize',13,'Color',[0.75 0.25 0.10]);
text(0,0.50,sprintf('真机对象 + 匹配ESO: %.4f / %.4f mH', ...
    lFromBd(3),lFromBq(3)),'FontSize',13,'Color',[0.05 0.50 0.25]);
text(0,0.36,sprintf('匹配后误差: %+.4f%% / %+.4f%%', ...
    errD(3),errQ(3)),'FontSize',13,'Color',[0.05 0.50 0.25]);
text(0,0.19,'b_0准确恢复T_s/L；R与反电动势仍混在a系数/ESO扰动中。', ...
    'FontSize',12,'Color',[0.20 0.20 0.20]);
text(0,0.08,'因此RLS递推有效，但ESO和回归结构必须与真机对象匹配。', ...
    'FontSize',12,'FontWeight','bold');

sgtitle('R2024b：ESOrls换用真机电机参数后的辨识结果');
exportgraphics(fig, fullfile(outputDir, 'summary_interpreted.png'), ...
    'Resolution',170);
close(fig);

interpreted = table(labels(:),trueL(:),lFromBd(:),lFromBq(:), ...
    errD(:),errQ(:), 'VariableNames', ...
    {'case_name','true_L_mH','L_from_bd_mH','L_from_bq_mH', ...
     'error_d_percent','error_q_percent'});
writetable(interpreted, fullfile(outputDir, 'summary_interpreted.csv'), ...
    'Encoding','UTF-8');
disp(interpreted);
