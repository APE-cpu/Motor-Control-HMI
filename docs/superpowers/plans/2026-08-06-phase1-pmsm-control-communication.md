# 阶段一 PMSM 控制与通信边界重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with checkpoints. Each task must finish its tests and commit before the next task begins.

**Goal:** 将上位机中间层收敛为 PMSM 控制与可审计通信，同时把设备档案、实体传感器一次性配置和负载/机械元数据迁移到正确的上下层，并保持 v1.8.0 的旧实验记录可读。

**Architecture:** 使用兼容迁移适配器保存旧字段语义；新增独立的实体传感器诊断页和负载配置组件；控制页只管理 PMSM 运行时控制策略与动态保护限值。真机通信由控制参数和诊断页显式传感器配置分别负责，负载类型永远不进入真机控制报文。

**Tech Stack:** Python、PySide6、dataclasses、现有 CommManager/协议编码器、pytest、Qt offscreen 测试。

## Global Constraints

- 新建配置只允许 PMSM；双凸极和直线电机旧配置必须可读但不可启动。
- CMD_SET_SENSOR 只能从“诊断与标定”页显式发送，并检查 ACK/超时/拒绝原因。
- 空载、负载电机、磁粉制动器等负载类型属于实验/数字孪生元数据，真机模式不得因其选择产生任何新增控制报文。
- v1.8.0 的实验记录、设备快照和旧 JSON 不得因字段迁移而崩溃；迁移必须幂等。
- 本阶段不删除旧 SRM 控制器文件，只切断新建、界面、运行时和新报文入口，并标记为遗留代码。
- 每个任务完成后运行该任务的测试并单独提交；失败只回滚最近一个提交。

---

## 文件地图

| 文件 | 职责 |
|---|---|
| config/config.py | PMSM-only 新建枚举、实体传感器/无位置算法分组和兼容常量 |
| experiments/phase1_migration.py | 旧快照/旧电机类型/旧负载字段的幂等归一化 |
| experiments/sensor_calibration.py | 实体传感器配置的原子 JSON 读写与校验状态 |
| widgets/load_setup_panel.py | 实验负载元数据、数字孪生负载模型和扰动控件 |
| pages/diagnostics_page.py | 实体传感器选择、详情、自检、校准和 CMD_SET_SENSOR |
| pages/control_page.py | PMSM 运行命令、控制策略、无位置算法和动态运行限值 |
| pages/experiment_page.py | 电机详情、设备档案、负载元数据、实验快照和预检 |
| main_window.py | 页面依赖注入、导航分组和稳定的命名索引 |
| experiments/models.py | 设备/会话快照中新增的传感器与负载字段兼容解析 |
| README.md、软件介绍.md、使用说明书.md、通信规约与数据字典.md | 当前支持边界和通信职责说明 |

---

### Task 1: 建立 PMSM-only 配置和旧快照迁移契约

**Files:**
- Create: experiments/phase1_migration.py
- Create: tests/test_phase1_migration.py
- Modify: config/config.py:15-60
- Modify: experiments/models.py:15-190
- Modify: experiments/__init__.py

**Interfaces:**
- Produces SUPPORTED_MOTOR_TYPE、PHYSICAL_POSITION_SENSORS、SENSORLESS_POSITION_METHODS。
- Produces normalize_motor_type(value: str) -> tuple[str, str | None]，返回 (normalized, legacy_raw)；PMSM 返回 ("PMSM", None)，其他旧类型返回 ("legacy", 原字符串)。
- Produces migrate_phase1_snapshot(snapshot: dict) -> dict，不修改输入对象，输出带 schema_version=2、motor_profile、load_setup、sensor_calibration 的副本。
- Produces migrate_phase1_session_data(data: dict) -> dict，负责把会话内的旧 device.extra/controller_params/protection_params 迁移到兼容字段。
- ExperimentSession.from_dict() 使用 migrate_phase1_session_data() 后再构造 DeviceProfile，旧数据仍接受缺失字段。

- [ ] **Step 1: 写迁移契约的失败测试**

    def test_旧电机类型被保留但不可运行():
        normalized, legacy = normalize_motor_type("双凸极电机")
        assert normalized == "legacy"
        assert legacy == "双凸极电机"

    def test_旧控制快照迁移为四类字段且不修改输入():
        source = {
            "device": {"motor_type": "永磁同步电机(PMSM)",
                       "extra": {"model": "PMSM-78"}},
            "controller_params": {
                "mechanical_load": {"load_type": "恒转矩负载", "load_value": 0.2},
            },
            "protection_params": {"max_rpm": 3200, "max_current_a": 1.6},
        }
        migrated = migrate_phase1_snapshot(source)
        assert migrated["schema_version"] == 2
        assert migrated["motor_profile"]["model"] == "PMSM-78"
        assert migrated["load_setup"]["kind"] == "恒转矩负载"
        assert migrated["protection_params"]["max_rpm"] == 3200
        assert "motor_profile" not in source

- [ ] **Step 2: 运行测试确认缺少实现**

Run: pytest tests/test_phase1_migration.py -q

Expected: FAIL，因为迁移常量和函数尚不存在。

- [ ] **Step 3: 实现最小兼容层**

将 MOTOR_TYPES 的新建值改为只含 PMSM；保留 LEGACY_MOTOR_TYPES 用于读取旧值。新增迁移函数时使用深拷贝，读取旧 device.extra、controller_params.mechanical_load、protection_params，并使用默认空字典补齐缺失字段。migrate_phase1_session_data() 负责会话顶层字段，migrate_phase1_snapshot() 负责控制页快照；ExperimentSession.from_dict() 先调用前者，再按现有枚举和 DeviceProfile.from_dict() 解析。

- [ ] **Step 4: 运行测试确认通过**

Run: pytest tests/test_phase1_migration.py tests/test_experiment_session.py -q

Expected: PASS；旧会话序列化测试的原有字段值不变。

- [ ] **Step 5: 提交**

    git add config/config.py experiments/phase1_migration.py experiments/models.py experiments/__init__.py tests/test_phase1_migration.py
    git commit -m "feat: add phase one PMSM migration contracts"

### Task 2: 抽取实验负载与数字孪生负载组件

**Files:**
- Create: widgets/load_setup_panel.py
- Create: tests/test_load_setup_panel.py
- Modify: pages/experiment_page.py:80-110
- Modify: pages/control_page.py:265-438,699-755
- Modify: tests/test_load_model.py

**Interfaces:**
- LoadSetupPanel(comm: CommManager, parent: QWidget | None = None)。
- LoadSetupPanel.compute_load(load_type_idx: int, value: float, rpm: float) -> float 保持现有仿真计算语义。
- LoadSetupPanel.snapshot() -> dict 返回 kind、value_nm、simulation_model、viscous_friction_B、coulomb_friction_Tc、inertia_J 和扰动字段。
- LoadSetupPanel.apply_snapshot(value: dict) -> None 只更新 UI，不发送控制报文。
- LoadSetupPanel.apply_to_simulation() -> None 只有 comm.is_sim_running() 为真时才调用现有 set_sim_load/pulse_sim_load/set_sim_load_disturbance。

- [ ] **Step 1: 写负载组件和真机隔离的失败测试**

    def test_负载类型映射和仿真计算保持原语义():
        assert LoadSetupPanel.compute_load(0, 5.0, 3000) == 0.0
        assert LoadSetupPanel.compute_load(1, 0.3, 500) == 0.3
        assert LoadSetupPanel.compute_load(2, 0.4, 2000) == pytest.approx(1.6)

    def test_真机模式应用负载不发送帧(monkeypatch):
        comm = CommManager()
        sent = []
        monkeypatch.setattr(comm, "send_frame",
                            lambda frame: sent.append(frame) or True)
        panel = LoadSetupPanel(comm)
        panel._load_type.setCurrentIndex(1)
        panel._load_value.setValue(0.2)
        panel.apply_to_simulation()
        assert sent == []

- [ ] **Step 2: 运行测试确认缺少实现**

Run: pytest tests/test_load_setup_panel.py -q

Expected: FAIL，因为组件尚不存在。

- [ ] **Step 3: 搬移现有机械控件**

从 ControlPage._build_load_box()、_compute_load()、_on_apply_mechanical()、_on_pulse() 和 _on_toggle_disturb() 提取到 LoadSetupPanel。实验负载类型下拉框使用四个实验语义值：空载、负载电机、磁粉制动器、其他机械负载；数字孪生模型另保留恒转矩/风机泵/对拖的内部计算选择。组件在真机模式显示“仅记录元数据”，不调用任何 send_frame。

- [ ] **Step 4: 将组件挂入实验管理并从控制页移除**

在 ExperimentPage.__init__() 中创建 self._load_setup = LoadSetupPanel(comm)，在配置区域加入该组件；启动实验时把 self._load_setup.snapshot() 写入会话快照。删除控制页负载控件和控制页快照中的 mechanical_load，短期保留 ControlPage._compute_load() 静态兼容包装并标记弃用，确保旧外部测试不会突然导入失败。

- [ ] **Step 5: 运行测试确认通过**

Run: pytest tests/test_load_setup_panel.py tests/test_load_model.py tests/test_experiment_page.py -q

Expected: PASS；仿真负载行为保持，控制页不存在负载选择，实验快照包含 load_setup。

- [ ] **Step 6: 提交**

    git add widgets/load_setup_panel.py pages/control_page.py pages/experiment_page.py tests/test_load_setup_panel.py tests/test_load_model.py tests/test_experiment_page.py
    git commit -m "refactor: move load setup to experiment management"

### Task 3: 建立实体位置传感器诊断与标定页

**Files:**
- Create: experiments/sensor_calibration.py
- Create: pages/diagnostics_page.py
- Create: tests/test_diagnostics_page.py
- Modify: widgets/sensor_detail_dialog.py:1-220
- Modify: config/config.py:45-62

**Interfaces:**
- SensorCalibrationStore(path: Path) 提供 load() -> dict 和 save(profile: dict) -> None，保存使用 .tmp + os.replace()。
- DiagnosticsPage(comm: CommManager, storage_path: Path | None = None)。
- DiagnosticsPage.snapshot() -> dict 返回 sensor_name、params、validated、validated_at、last_result。
- DiagnosticsPage.selected_sensor() -> str 只返回 Hall/QEP/Resolver。
- DiagnosticsPage.apply_sensor_config() -> bool 是 CMD_SET_SENSOR 的唯一 UI 触发函数。

- [ ] **Step 1: 写实体传感器范围、原子保存和报文入口测试**

    def test_诊断页只显示实体传感器():
        page = DiagnosticsPage(CommManager())
        names = [page._sensor_combo.itemText(i)
                 for i in range(page._sensor_combo.count())]
        assert names == ["霍尔传感器(Hall)", "增量式编码器(QEP)",
                         "旋转变压器(Resolver)"]

    def test_诊断页应用传感器才发送_set_sensor(monkeypatch):
        comm = CommManager()
        frames = []
        monkeypatch.setattr(comm, "send_frame",
                            lambda frame: frames.append(frame) or True)
        page = DiagnosticsPage(comm)
        assert page.apply_sensor_config() is True
        assert frames and decode_frame(frames[0])[0] == CMD_SET_SENSOR

- [ ] **Step 2: 运行测试确认缺少实现**

Run: pytest tests/test_diagnostics_page.py -q

Expected: FAIL，因为页面和存储类尚不存在。

- [ ] **Step 3: 实现传感器配置存储**

使用现有 runtime_paths.writable_path("config", "sensor_calibration.json") 作为默认路径；保存前校验 sensor_name 属于实体传感器且 params 为字典。写入失败不得覆盖旧文件；读取损坏 JSON 时返回未验证空配置并在页面显示错误。

- [ ] **Step 4: 搬移传感器面板和自检入口**

把 Hall/QEP/Resolver 面板从控制页的 _sensor_param_stack 搬到诊断页。详情按钮只传实体传感器名称给 SensorDetailDialog；保留 3 秒遥测自检逻辑，但把“请在控制页选中”提示改为“请在诊断页应用配置”。无位置算法不出现在此页。

- [ ] **Step 5: 实现显式下发与验证状态**

apply_sensor_config() 调用现有 SENSOR_REGISTRY 和 encode_frame(CMD_SET_SENSOR, payload)，发送成功后保存 validated=False 并等待命令结果；收到 ACK 后才写 validated=True 和时间戳。超时/NACK 保持未验证，并显示设备返回原因。

- [ ] **Step 6: 运行测试确认通过**

Run: pytest tests/test_diagnostics_page.py tests/test_comm_virtual_v2.py -q

Expected: PASS；原有 CMD_SET_SENSOR 编解码行为不变。

- [ ] **Step 7: 提交**

    git add experiments/sensor_calibration.py pages/diagnostics_page.py widgets/sensor_detail_dialog.py config/config.py tests/test_diagnostics_page.py
    git commit -m "feat: add physical sensor diagnostics page"

### Task 4: 将控制页收敛为 PMSM 控制与无位置算法

**Files:**
- Modify: pages/control_page.py
- Modify: pages/control_param_panels/panels.py:349-382
- Modify: config/config.py:18-58
- Modify: controllers/sensorless_controller.py
- Create: tests/test_control_page_pmsm_only.py
- Modify: tests/test_ai_profiles_and_real_defaults.py

**Interfaces:**
- ControlPage(comm, state_machine=None, sensor_snapshot_provider: Callable[[], dict] | None = None)。
- ControlPage.set_sensor_snapshot_provider(provider) 允许主窗口在创建页面后注入诊断页。
- ControlPage.runtime_limits_snapshot() -> dict 返回 max_rpm、max_current_a。
- ControlPage.set_runtime_limits(max_rpm: float | None = None, max_current_a: float | None = None) -> None 只更新本次控制运行限值，不修改电机档案。
- ControlPage.experiment_snapshot() 只输出 controller_params、protection_params 和已验证的位置源摘要，不再从控制页生成设备详情或负载配置。

- [ ] **Step 1: 写 PMSM-only 和无隐式传感器报文测试**

    def test_控制页不再暴露电机类型详情和负载():
        page = ControlPage(CommManager())
        assert not hasattr(page, "_motor_type")
        assert not hasattr(page, "_load_type")
        assert not hasattr(page, "_sensor_tree")
        assert not hasattr(page, "_sensor_param_stack")
        assert "电流斩波控制(CCC)" not in [
            page._mode_combo.itemText(i)
            for i in range(page._mode_combo.count())
        ]

    def test_应用控制参数不发送传感器配置(monkeypatch):
        comm = CommManager()
        frames = []
        monkeypatch.setattr(comm, "send_frame",
                            lambda frame: frames.append(frame) or True)
        page = ControlPage(
            comm,
            sensor_snapshot_provider=lambda: {
                "sensor_name": "增量式编码器(QEP)", "validated": True,
            })
        page._on_apply()
        assert all(decode_frame(frame)[0] != CMD_SET_SENSOR for frame in frames)

- [ ] **Step 2: 运行测试确认当前实现失败**

Run: pytest tests/test_control_page_pmsm_only.py -q

Expected: FAIL，因为当前控制页仍创建三类电机、SRM 控制器、实体传感器树和负载控件。

- [ ] **Step 3: 删除运行时入口并保留遗留文件**

从 _MODE_REGISTRY、导入列表和模式说明中移除 CurrentChoppingController、AnglePositionController、VoltageControlController 及其面板；PMSM_CONTROL_MODES 增加“无位置传感器控制”。旧控制器文件保留，不再被 ControlPage 导入。

- [ ] **Step 4: 重建顶部控制区**

将 _build_motor_box() 改为“运行约束与保护”，只保留 _max_rpm、_current_limit、固件保护回读和当前设备能力状态。移除电机类型、型号、极对数、额定温度和电机详情按钮。实体传感器树、实体传感器参数栈和详情按钮全部移除；新增只读“已验证位置源”摘要。

- [ ] **Step 5: 保留并增强无位置算法说明**

把 _SENSORLESS_FORMULA 的说明改为“控制算法”语义，显示 SMO/EKF/MRAS/HFI 的适用速度、输入量、主要参数和低速失效提示。SensorlessPanel 仍在控制页的参数栈中；选择“无位置传感器控制”时只更新 SensorlessController 参数，不触发 CMD_SET_SENSOR。

- [ ] **Step 6: 改造应用和快照路径**

_on_apply() 只读取当前 PMSM 控制面板、运行限值和诊断页提供的位置源；物理传感器配置不再编码进 CMD_SET_SENSOR。控制报文保留必要的控制模式/算法参数和 max_current_a，不包含 load_type、J/Tc/B 或电机详情。experiment_snapshot() 输出控制器参数、运行约束和位置源摘要。

- [ ] **Step 7: 运行控制页和回归测试**

Run: pytest tests/test_control_page_pmsm_only.py tests/test_ai_profiles_and_real_defaults.py tests/test_safety_reset_manual.py tests/test_runtime_state.py -q

Expected: PASS；PI 参数方案、停止/故障状态机和电流限幅行为不回归，控制页不再出现 SRM/直线电机入口。

- [ ] **Step 8: 提交**

    git add config/config.py pages/control_page.py pages/control_param_panels/panels.py controllers/sensorless_controller.py tests/test_control_page_pmsm_only.py tests/test_ai_profiles_and_real_defaults.py
    git commit -m "refactor: make control page PMSM only"

### Task 5: 把电机详情、设备档案和实验快照归实验管理

**Files:**
- Modify: pages/experiment_page.py
- Modify: experiments/models.py
- Modify: experiments/report.py
- Modify: widgets/motor_info_dialog.py
- Create: tests/test_experiment_phase1_snapshot.py
- Modify: tests/test_experiment_page.py

**Interfaces:**
- ExperimentPage(..., sensor_snapshot_provider: Callable[[], dict] | None = None, runtime_limits_sink: Callable[..., None] | None = None)。
- ExperimentPage.motor_profile_snapshot() -> dict 从 load_motor_info() 读取并返回不含运行时限值的电机档案。
- ExperimentPage.experiment_snapshot() -> dict 合并控制快照、设备档案、传感器校准摘要和 LoadSetupPanel.snapshot()。
- ExperimentPage._run_precheck() 只以 protection_params.max_rpm/max_current_a 作为本次运行约束，以设备档案安全上限作为独立上限校验。

- [ ] **Step 1: 写实验快照新归属测试**

    def test_实验快照把详情和负载归实验管理(tmp_path, monkeypatch):
        monkeypatch.setattr("pages.experiment_page.load_motor_info", lambda: {
            "model": "PMSM-78", "pole_pairs": 4,
            "rated": {"power_W": 78, "temperature_C": 65},
            "measured": {"Rs_ohm": 1.2}, "description": "实验室电机",
        })
        page = ExperimentPage(
            CommManager(), storage_root=tmp_path / "records",
            snapshot_provider=lambda: {
                "controller_params": {"control_mode": "闭环PI控制"},
                "protection_params": {"max_rpm": 3200, "max_current_a": 1.6},
            },
            sensor_snapshot_provider=lambda: {
                "sensor_name": "增量式编码器(QEP)", "validated": True,
            },
        )
        snap = page.experiment_snapshot()
        assert snap["motor_profile"]["model"] == "PMSM-78"
        assert snap["load_setup"]["kind"] == "空载"
        assert snap["protection_params"]["max_rpm"] == 3200
        assert snap["sensor_calibration"]["validated"] is True

- [ ] **Step 2: 运行测试确认缺少接口**

Run: pytest tests/test_experiment_phase1_snapshot.py -q

Expected: FAIL，因为实验页还没有电机档案按钮、负载快照和传感器提供者接口。

- [ ] **Step 3: 在实验管理增加电机详情入口**

在设备组合档案区域加入“编辑电机详情”按钮，按钮打开现有 MotorInfoDialog。保存后只刷新实验管理的设备摘要；控制页不再读取该对话框中的额定电流、最高转速或温度。MotorInfoDialog 的字段继续保留额定/实测/描述，但不得再保存 max_rpm 或运行电流限幅。

- [ ] **Step 4: 合并实验快照并迁移旧字段**

_on_start() 创建 DeviceProfile 时把电机详情放入 extra.motor_profile，把诊断页快照放入 extra.sensor_calibration，把负载面板快照放入 extra.load_setup；controller_params 只保存控制策略和位置源，protection_params 保存本次运行限值。读取旧会话时通过 Task 1 的迁移函数兼容旧 mechanical_load 和 device.extra.max_rpm。

- [ ] **Step 5: 修正实验预检和报告字段**

预检顺序固定为：通信/数字孪生存在 → 设备类型为 PMSM → 位置源已验证 → 控制方式存在 → 目标转速不超过运行限值、模板限值和设备硬上限 → 当前电流/母线/温度未越界。报告中增加“设备档案、控制运行约束、传感器校准状态、负载元数据”四个小节，并保留旧报告读取兼容。

- [ ] **Step 6: 运行测试确认通过**

Run: pytest tests/test_experiment_phase1_snapshot.py tests/test_experiment_page.py tests/test_experiment_report.py tests/test_experiment_session.py -q

Expected: PASS；旧测试中的设备名称、控制模式、限幅和会话生命周期断言仍成立，新断言确认负载与电机详情由实验页拥有。

- [ ] **Step 7: 提交**

    git add pages/experiment_page.py experiments/models.py experiments/report.py widgets/motor_info_dialog.py tests/test_experiment_phase1_snapshot.py tests/test_experiment_page.py
    git commit -m "refactor: make experiment management own equipment snapshots"

### Task 6: 接入诊断页并重排稳定导航

**Files:**
- Modify: main_window.py:90-150
- Modify: pages/communication_page.py
- Create: tests/test_main_window_phase1.py

**Interfaces:**
- MainWindow.page_indices: dict[str, int] 保存页面名称到 stack 索引，导航不再依赖 sampling_idx 等算术常量。
- MainWindow 创建顺序为 diagnostics_page -> control_page(sensor_snapshot_provider=...) -> experiment_page(...).

- [ ] **Step 1: 写导航和依赖注入测试**

    def test_主窗口包含诊断页并按名称定位():
        window = MainWindow(enable_training=False)
        assert "diagnostics" in window.page_indices
        assert window.stack.widget(window.page_indices["diagnostics"]) is window.diagnostics_page
        assert window.stack.widget(window.page_indices["control"]) is window.control_page

- [ ] **Step 2: 运行测试确认当前导航缺少诊断页**

Run: pytest tests/test_main_window_phase1.py -q

Expected: FAIL，因为当前主窗口没有 diagnostics_page 和命名索引。

- [ ] **Step 3: 加入诊断页和 provider wiring**

在主窗口创建共享 CommManager 后先创建 DiagnosticsPage，再把 diagnostics_page.snapshot 注入 ControlPage 和 ExperimentPage。控制页的参数应用、实验页的预检都只能通过该 provider 读取传感器摘要。

- [ ] **Step 4: 用命名索引重建导航**

每次 stack.addWidget(page) 后记录 self.page_indices[name]。导航新增“诊断与标定”，放在“设备与通信”分组；保留通信设置、电流采样诊断和原有 AI/实验入口，不再手工计算页面索引。

- [ ] **Step 5: 运行 GUI smoke test**

Run: pytest tests/test_main_window_phase1.py tests/test_current_sampling_diag.py -q

Expected: PASS；窗口可以在 offscreen 下创建，所有页面索引唯一，关闭窗口不抛异常。

- [ ] **Step 6: 提交**

    git add main_window.py pages/communication_page.py tests/test_main_window_phase1.py
    git commit -m "feat: wire diagnostics page and named navigation"

### Task 7: 清理文档、状态摘要和遗留入口

**Files:**
- Modify: README.md
- Modify: 软件介绍.md
- Modify: 使用说明书.md
- Modify: 通信规约与数据字典.md
- Modify: pages/communication_page.py
- Modify: logs/operation_logger.py（仅更新事件名称映射）
- Create: tests/test_phase1_support_surface.py

**Interfaces:**
- 文档和 UI 的“当前支持矩阵”统一写为 PMSM；SRM/直线电机只能在兼容说明中出现。
- 通信文档明确：CMD_SET_SENSOR 属于诊断页；负载类型和机械参数是实验/数字孪生字段，不是下位机控制字段。

- [ ] **Step 1: 写支持面回归测试**

    def test_新建支持面不暴露_srm_和直线电机():
        from config.config import MOTOR_TYPES, CONTROL_MODES_BY_MOTOR
        assert MOTOR_TYPES == ["永磁同步电机(PMSM)"]
        assert set(CONTROL_MODES_BY_MOTOR) == {"永磁同步电机(PMSM)"}

- [ ] **Step 2: 更新文档和通信说明**

删除面向新用户的“支持双凸极/直线电机”表述，保留“旧记录兼容但不可运行”说明；把负载选择、设备档案、传感器校准和控制参数的归属写入使用说明书的实验流程。

- [ ] **Step 3: 运行支持面测试**

Run: pytest tests/test_phase1_support_surface.py -q

Expected: PASS；rg -n "支持.*双凸极|支持.*直线电机|负载类型.*CMD_SET|控制页.*传感器自检" README.md 软件介绍.md 使用说明书.md 通信规约与数据字典.md 只命中兼容说明或历史章节。

- [ ] **Step 4: 提交**

    git add README.md 软件介绍.md 使用说明书.md 通信规约与数据字典.md pages/communication_page.py logs/operation_logger.py tests/test_phase1_support_surface.py
    git commit -m "docs: document PMSM control and communication boundaries"

### Task 8: 完成全量验证和回滚材料检查

**Files:**
- Modify only if a test exposes a regression: the specific file named by pytest.
- Create: tests/test_phase1_no_load_frame.py if the frame assertion needs an isolated fixture.

- [ ] **Step 1: 运行分层测试**

    pytest tests/test_phase1_migration.py tests/test_load_setup_panel.py tests/test_diagnostics_page.py -q
    pytest tests/test_control_page_pmsm_only.py tests/test_experiment_phase1_snapshot.py tests/test_main_window_phase1.py -q

Expected: 两条命令均 PASS。

- [ ] **Step 2: 运行全量测试和导入检查**

    pytest -q
    python -c "import main_window; from pages.diagnostics_page import DiagnosticsPage; print('imports ok')"

Expected: 全量 pytest PASS；导入检查输出 imports ok，无 SRM/直线电机运行时导入。

- [ ] **Step 3: 做无硬件真机报文审计**

使用 CommManager 的 spy transport 创建真机模式 LoadSetupPanel、ControlPage 和 DiagnosticsPage：负载操作发送 0 帧，控制参数发送只包含 CMD_SET_PARAMS，诊断应用才发送 CMD_SET_SENSOR。保存审计输出到测试断言，不写用户配置。

- [ ] **Step 4: 检查工作树和回滚点**

    git status --short
    git log --oneline -10

Expected: 每个阶段提交均可见，工作树只保留明确的测试修复；任一阶段可通过 git revert <该阶段提交> 回滚，不触碰实验记录目录。

- [ ] **Step 5: 提交最后的测试修复**

    git add tests
    git commit -m "test: verify phase one control communication boundaries"
