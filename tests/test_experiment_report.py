from communications.comm_manager import TelemetryFrame
from experiments import (
    DeviceProfile, ExperimentReportGenerator, ExperimentSessionManager,
    ExperimentTemplateRepository,
)


def test_生成Markdown_HTML和SVG完整实验报告(tmp_path):
    manager = ExperimentSessionManager(tmp_path / "records")
    template = ExperimentTemplateRepository(tmp_path / "templates").list_templates()[0]
    session = manager.create_session(
        "报告转速阶跃",
        purpose="验证报告完整性",
        operator="tester",
        device=DeviceProfile("78W PMSM", rated_power_w=78, dc_bus_voltage_v=48),
        controller_params={"control_mode": "PI", "target_speed_rpm": 1200},
        protection_params={"max_rpm": 3000},
        template=template,
        notes="达到预期，下一步验证负载阶跃。",
    )
    manager.start()
    manager.capture_runtime_context("start", {
        "runtime_state": "ready",
        "protocol": {"mode": "virtual-v2", "statistics": {"tx_frames": 3}},
    })
    for index, speed in enumerate((0.0, 800.0, 1190.0)):
        frame = TelemetryFrame()
        frame.speed_actual = speed
        frame.speed_target = 1200.0
        frame.current_actual = 0.5 + index
        frame.current_target = 2.5
        frame.vdc = 48.0 + index * 0.1
        frame.temperature = 25.0 + index
        if index == 1:
            frame.fault_code = 0x02
            frame.fault_text = "测试过流瞬态"
        manager.record_telemetry(frame)
    manager.record_event(
        "operation", "运行状态切换", {"detail": "ready → running：设备ACK启动"})
    manager.record_event("operation", "故障观察", {"detail": "无故障"})
    manager.record_marker(
        "load_applied", "负载投入",
        {"speed_actual": 800.0, "current_actual": 1.5}, "加载0.2 N·m")
    for _ in template.steps:
        manager.confirm_current_step("步骤验证完成")
    manager.capture_runtime_context("end", {
        "runtime_state": "ready",
        "protocol": {"mode": "virtual-v2", "statistics": {"tx_frames": 20}},
    })
    manager.complete()
    manager.update_conclusion(session.experiment_id, {
        "result_status": "passed",
        "observations": "转速在允许时间内恢复",
        "anomalies": "存在轻微超调",
        "recommendations": "保持当前PI参数",
        "next_plan": "进行负载阶跃",
    })

    paths = ExperimentReportGenerator(manager.repository).generate(session.experiment_id)

    markdown = paths.markdown.read_text(encoding="utf-8")
    html = paths.html.read_text(encoding="utf-8")
    svg = paths.svg.read_text(encoding="utf-8")
    assert "报告转速阶跃" in markdown
    assert "野火 78W PMSM 真机基础运行实验" in markdown
    assert "步骤验证完成" in markdown
    assert "转速平均绝对跟踪误差" in markdown
    assert "运行状态切换" in markdown
    assert "tx_frames" in markdown
    assert "测试过流瞬态" in markdown
    assert "负载投入" in markdown
    assert "转速在允许时间内恢复" in markdown
    assert "达到目的" in markdown
    assert "达到预期" in markdown
    assert "![遥测曲线](telemetry.svg)" in markdown
    assert "<svg" in html and "telemetry.svg" not in html
    assert "<polyline" in svg
    assert "stroke-dasharray" in svg


def test_报告支持无遥测且事件坏行隔离(tmp_path):
    manager = ExperimentSessionManager(tmp_path / "records")
    session = manager.create_session("无遥测实验")
    manager.start()
    manager.complete()
    event_path = manager.repository.session_dir(session.experiment_id) / "events.jsonl"
    with open(event_path, "a", encoding="utf-8") as stream:
        stream.write("{broken\n")

    paths = ExperimentReportGenerator(manager.repository).generate(session.experiment_id)

    assert "本实验没有遥测数据" in paths.markdown.read_text(encoding="utf-8")
    assert "无遥测数据" in paths.svg.read_text(encoding="utf-8")
    assert len(manager.repository.read_events(session.experiment_id)) == 2
