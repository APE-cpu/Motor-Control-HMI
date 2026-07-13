"""结构化实验结论编辑器。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QPlainTextEdit,
    QVBoxLayout,
)


class ExperimentConclusionDialog(QDialog):
    def __init__(self, experiment_id: str, conclusion: dict | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"编辑实验结论 - {experiment_id}")
        self.resize(680, 560)
        values = conclusion or {}
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.result_status = QComboBox()
        self.result_status.addItem("待判断", "pending")
        self.result_status.addItem("达到目的", "passed")
        self.result_status.addItem("部分达到", "partial")
        self.result_status.addItem("未达到/失败", "failed")
        index = self.result_status.findData(values.get("result_status", "pending"))
        self.result_status.setCurrentIndex(max(0, index))
        self.observations = self._editor("主要观察结果", values.get("observations", ""))
        self.anomalies = self._editor("异常与可能原因", values.get("anomalies", ""))
        self.recommendations = self._editor("参数或方案建议", values.get("recommendations", ""))
        self.next_plan = self._editor("下一次实验计划", values.get("next_plan", ""))
        form.addRow("结论状态", self.result_status)
        form.addRow("主要观察结果", self.observations)
        form.addRow("异常与原因", self.anomalies)
        form.addRow("改进建议", self.recommendations)
        form.addRow("下一步计划", self.next_plan)
        root.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存结论")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _editor(placeholder: str, value: str) -> QPlainTextEdit:
        editor = QPlainTextEdit(str(value or ""))
        editor.setPlaceholderText(placeholder)
        editor.setMaximumHeight(90)
        return editor

    def conclusion(self) -> dict:
        return {
            "result_status": self.result_status.currentData(),
            "observations": self.observations.toPlainText(),
            "anomalies": self.anomalies.toPlainText(),
            "recommendations": self.recommendations.toPlainText(),
            "next_plan": self.next_plan.toPlainText(),
        }
