import json

import pytest

from experiments import (
    ExperimentSessionManager, ExperimentTemplateRepository, WorkflowStep,
)


def test_模板仓库提供内置方案并持久化用户模板(tmp_path):
    repository = ExperimentTemplateRepository(tmp_path / "templates")
    built_in = repository.list_templates()[0]
    assert built_in.built_in is True
    assert built_in.device_defaults["rated_power_w"] == 78.0
    assert len(built_in.steps) >= 6

    created = repository.create(
        "自定义阶跃",
        purpose="验证模板持久化",
        steps=[WorkflowStep("S01", "确认参数"),
               WorkflowStep("S02", "观察波形", required=False)],
    )
    loaded = repository.load(created.template_id)
    assert loaded == created
    assert [item.name for item in repository.list_templates()] == [
        built_in.name, "自定义阶跃"]

    repository.delete(created.template_id)
    assert len(repository.list_templates()) == 1
    with pytest.raises(ValueError, match="内置模板不能删除"):
        repository.delete(built_in.template_id)


def test_模板在实验创建时冻结且步骤进入事件审计(tmp_path):
    templates = ExperimentTemplateRepository(tmp_path / "templates")
    template = templates.create(
        "两步流程",
        steps=[WorkflowStep("S01", "必做检查"),
               WorkflowStep("S02", "可选观察", required=False)],
    )
    manager = ExperimentSessionManager(tmp_path / "records")
    session = manager.create_session("模板实验", template=template)
    manager.start()

    # 创建后再修改模板，当前实验仍使用冻结版本。
    template.steps[0].title = "已被修改的标题"
    templates.save(template)
    assert manager.current_workflow_step().title == "必做检查"
    with pytest.raises(RuntimeError, match="必做步骤"):
        manager.complete()

    manager.confirm_current_step("参数与接线一致")
    with pytest.raises(ValueError, match="必须填写原因"):
        manager.skip_current_step("")
    manager.skip_current_step("本次不做附加观察")
    completed = manager.complete()

    assert completed.workflow_completed_steps == ["S01"]
    assert completed.workflow_current_index == 2
    directory = manager.repository.session_dir(session.experiment_id)
    frozen = json.loads(
        (directory / "template_snapshot.json").read_text(encoding="utf-8"))
    assert frozen["steps"][0]["title"] == "必做检查"
    events = [json.loads(line) for line in
              (directory / "events.jsonl").read_text("utf-8").splitlines()]
    assert [event["type"] for event in events] == [
        "session_started", "workflow_step_confirmed",
        "workflow_step_skipped", "session_completed",
    ]
    assert events[1]["details"]["note"] == "参数与接线一致"


def test_必做步骤不能跳过且重复编号被拒绝(tmp_path):
    repository = ExperimentTemplateRepository(tmp_path / "templates")
    with pytest.raises(ValueError, match="步骤编号重复"):
        repository.create(
            "坏模板",
            steps=[WorkflowStep("S01", "A"), WorkflowStep("S01", "B")],
        )
    template = repository.create("单步", steps=[WorkflowStep("S01", "检查")])
    manager = ExperimentSessionManager(tmp_path / "records")
    manager.create_session("实验", template=template)
    manager.start()
    with pytest.raises(RuntimeError, match="必做步骤不能跳过"):
        manager.skip_current_step("想跳过")
