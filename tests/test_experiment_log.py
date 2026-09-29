import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HMI_NO_WEBENGINE"] = "1"

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from ai.experiment_tools import create_experiment_registry
from ai.harness import AuditLog, ToolCall, ToolContext, ToolExecutor
from core.phasor_decomposition import synthetic_capture
from core.power_estimate import (
    PowerParams, estimate_power_series, park_currents, power_summary,
)
from core.vector_distortion import DistortionParams
from experiments.experiment_log import (
    AI_FILE, LOG_HTML, ExperimentLogGenerator, comparison_metrics, draft_ai_conclusion,
)
from experiments.session_manager import ExperimentSessionManager


def _app():
    return QApplication.instance() or QApplication([])


def _write_capture(path, kp_gain=1.0, seconds=1.0):
    """合成 16 kHz 高速数据（与固件导出列一致），电流按固件角度约定 iq 沿 θ。"""
    capture = synthetic_capture(DistortionParams(offset_a_pct=3.0, gain_b_pct=4.0),
                                fe_hz=50.0, duration_s=seconds, amplitude_a=2.0)
    # 合成数据的电流矢量沿 θ；固件角度指向 q 轴，二者一致
    angle = np.degrees(capture.theta) % 360.0
    _i_d, iq = park_currents(capture.ia, capture.ib, capture.theta)
    vq_raw = (0.59 * iq + 2 * np.pi * 50 * 7.04e-3) / (24.0 / np.sqrt(3)) * 32767
    rows = ["sample_index,time_s,angle_deg,speed_rpm,iq_a,iqref_a,ia_a,ib_a,vd_raw,vq_raw,vbus_v"]
    for i in range(capture.time.size):
        rows.append(f"{i},{capture.time[i]:.7f},{angle[i]:.4f},{750 * kp_gain:.2f},{iq[i]:.5f},"
                    f"2.0,{capture.ia[i]:.5f},{capture.ib[i]:.5f},0,{vq_raw[i]:.1f},24")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\ufeff" + "\n".join(rows), encoding="utf-8")


@pytest.fixture()
def experiments(tmp_path):
    manager = ExperimentSessionManager(tmp_path / "records")
    ids = []
    for name, kp, gain in (("基准", 1752, 1.0), ("Kp+10%", 1927, 1.01)):
        session = manager.create_session(name, data_source="real",
                                         controller_params={"kp_spd": kp, "ki_spd": 121})
        manager.start()
        manager.record_telemetry({"speed_actual": 750, "speed_target": 750, "vdc": 24.0})
        folder = manager.repository.session_dir(session.experiment_id) / "waveforms" / "b1"
        _write_capture(folder / "高速数据.csv", gain)
        manager.complete()
        ids.append(session.experiment_id)
    return manager, ids


def test_power_estimate_balances_on_synthetic_capture(tmp_path):
    path = tmp_path / "高速数据.csv"
    _write_capture(path)
    from core.phasor_decomposition import load_capture
    capture = load_capture(path)
    i_d, iq = park_currents(capture.ia, capture.ib, capture.theta)
    assert abs(np.mean(i_d)) < 0.05 and np.mean(iq) == pytest.approx(2.0, abs=0.1)
    summary = power_summary(estimate_power_series(capture.time, capture.columns(), PowerParams()))
    mean = summary["mean_w"]
    # 合成电压按 Rs·iq + ωe·ψf 构造：逆变器输入 ≈ 铜损 + 反电势功率
    assert mean["inv"] == pytest.approx(mean["cu"] + 1.5 * 2 * np.pi * 50 * 7.04e-3 * 2.0, rel=0.05)
    assert mean["cu"] == pytest.approx(1.5 * 0.59 * 4.0, rel=0.1)


def test_tools_are_read_only_scoped_and_audited(experiments, tmp_path):
    manager, ids = experiments
    registry, _data = create_experiment_registry(manager.repository)
    audit = AuditLog(tmp_path / "audit.jsonl")
    executor = ToolExecutor(registry, ToolContext(None, None), audit_log=audit)
    stats = executor.execute(ToolCall("1", "capture_statistics", {"experiment_id": ids[0]}))
    assert stats.ok and stats.metadata["source"]["samples"] == 16000
    assert len(stats.metadata["source"]["sha256"]) == 64
    vector = executor.execute(ToolCall("2", "decompose_current_vector", {"experiment_id": ids[0]}))
    assert vector.ok and vector.data["components"][0]["key"] == "k+1"
    diff = executor.execute(ToolCall("3", "compare_parameters",
                                     {"baseline_id": ids[0], "experiment_id": ids[1]}))
    assert diff.data["changed"] == [{"parameter": "controller.kp_spd",
                                     "baseline": 1752, "value": 1927}]
    # 不接受任意路径：多余参数被拒绝
    bad = executor.execute(ToolCall("4", "capture_statistics",
                                    {"experiment_id": ids[0], "path": "C:/Windows/win.ini"}))
    assert not bad.ok and "未知参数" in bad.error
    assert all(spec.permission == "read_only" for spec in map(registry.get, registry.names))
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4


def test_single_log_has_fixed_sections_and_provenance(experiments):
    _app()
    manager, ids = experiments
    generator = ExperimentLogGenerator(manager.repository)
    result = generator.generate(ids[0], make_pdf=False)
    text = result.html.read_text(encoding="utf-8")
    titles = ("实验概览", "过程与响应", "电流与频谱", "矢量与分解", "功率与假设",
              "设备与参数", "事件时间线", "波形与影音", "结论", "附件与来源")
    for number, title in enumerate(titles, 1):
        assert f'<div class="section-no">{number:02d}</div><div><h2>{title}' in text
    assert "AI 分析 <span class=\"tag ai\">未生成</span>" in text
    assert "技术结论待填写" in text                     # 结论为空不生成“通过”之类的判断
    assert "图 01" in text and "\x00" not in text      # 图号已按出现顺序编号
    assert "@bottom-right" in text                     # PDF 页码
    for name in ("频谱.png", "滑动频谱.gif", "滑动频谱_打印.png", "时频_iq.png", "电流圆.png",
                 "矢量合成.gif", "矢量合成_打印.png", "矢量合成_跟随.gif", "分量柱状图.png",
                 "功率流.gif", "功率流_平均.png"):
        assert (result.folder / "log_assets" / name).exists()
    # 打印版用静态图替代动图
    assert 'class="print-only" src="log_assets/功率流_平均.png"' in text
    evidence = json.loads((result.folder / "log_data.json").read_text(encoding="utf-8"))
    assert evidence["high_rate"]["power"]["ok"]
    assert (result.folder / "log_audit.jsonl").exists()


def test_log_without_high_rate_data_explains_how_to_get_it(tmp_path):
    _app()
    manager = ExperimentSessionManager(tmp_path / "records")
    session = manager.create_session("只有慢遥测")
    manager.start()
    manager.complete()
    result = ExperimentLogGenerator(manager.repository).generate(session.experiment_id,
                                                                 make_pdf=False)
    text = result.html.read_text(encoding="utf-8")
    assert "保存所有波形" in text and result.warnings


def test_comparison_log_lists_parameter_changes_and_deltas(experiments):
    _app()
    manager, ids = experiments
    generator = ExperimentLogGenerator(manager.repository)
    result = generator.generate_comparison(ids[0], [ids[1]], make_pdf=False)
    text = result.html.read_text(encoding="utf-8")
    assert "controller.kp_spd" in text and "1927" in text
    evidence = json.loads((result.folder / "log_data.json").read_text(encoding="utf-8"))
    base, other = (comparison_metrics(run) for run in evidence["runs"])
    assert other["转速均值 rpm"] == pytest.approx(base["转速均值 rpm"] * 1.01, rel=1e-3)
    assert "(+1.0%)" in text.replace("（", "(").replace("）", ")")


class _FakeClient:
    """模拟 OpenAI 兼容客户端：第一轮调用工具，第二轮给出结论。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return {"content": None, "tool_calls": [{
                "id": "c1", "function": {"name": "capture_statistics",
                                         "arguments": json.dumps({"experiment_id": self.eid})}}]}
        return {"content": "主要观察：转速均值约 750 rpm（capture_statistics）。"}


def test_ai_conclusion_is_separate_and_marked(experiments):
    _app()
    manager, ids = experiments
    generator = ExperimentLogGenerator(manager.repository)
    result = generator.generate(ids[0], make_pdf=False)
    client = _FakeClient()
    client.eid = ids[0]
    draft = draft_ai_conclusion(client, generator, result.folder, "fake-model")
    assert "capture_statistics" in draft["text"] and len(draft["evidence_sha256"]) == 64
    assert (result.folder / AI_FILE).exists()
    # 操作者结论不受影响
    assert manager.repository.load(ids[0]).conclusion == {}
    text = generator.generate(ids[0], make_pdf=False, reuse_assets=True).html.read_text(
        encoding="utf-8")
    assert "AI 分析 <span class=\"tag ai\">需人工确认</span>" in text and "fake-model" in text


def test_log_page_generates_single_and_comparison(experiments):
    _app()
    from pages.experiment_log_page import ExperimentLogPage
    manager, ids = experiments
    page = ExperimentLogPage(manager.repository)
    page.set_selection([ids[0]])
    assert page.generate()
    assert page.busy and not page._btn_generate.isEnabled()   # 后台生成，界面不阻塞
    assert page.wait_idle()
    assert (manager.repository.session_dir(ids[0]) / "report" / LOG_HTML).exists()
    page._template.setCurrentIndex(1)
    page.set_selection(ids, baseline=ids[0])
    assert sorted(page.selected_ids()) == sorted(ids)      # 列表按时间倒序
    assert page.generate()
    assert page.wait_idle()
    assert "_comparisons" in str(page._folder)
