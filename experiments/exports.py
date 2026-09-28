"""Link manually saved data to an experiment without sending device commands."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .session_manager import ExperimentSessionManager, _iso_now


@dataclass
class ExportTicket:
    experiment_id: str
    directory: Path
    standalone: ExperimentSessionManager | None = None


class ExperimentExports:
    def __init__(self, manager: ExperimentSessionManager):
        self.manager = manager

    def prepare(self, category: str, metadata: dict, snapshot: dict) -> ExportTicket:
        session = self.manager.active_session
        standalone = None
        if session is None:
            standalone = ExperimentSessionManager(self.manager.repository.root)
            session = standalone.create_session(**metadata)
            standalone.start()
        stamp = datetime.now().strftime("%H%M%S_%f")
        directory = (self.manager.repository.session_dir(session.experiment_id)
                     / "waveforms" / f"{stamp}_{uuid4().hex[:6]}")
        ticket = ExportTicket(session.experiment_id, directory, standalone)
        try:
            directory.mkdir(parents=True, exist_ok=False)
            self.manager.repository._write_json(directory / "export.json", {
                "experiment_id": session.experiment_id, "category": category,
                "created_at": _iso_now(), "status": "saving",
                "snapshot": snapshot,
            })
        except Exception:
            if standalone is not None:
                standalone.abort("无法建立数据保存目录")
            raise
        return ticket

    def finish(self, ticket: ExportTicket, result: dict) -> None:
        import json
        path = ticket.directory / "export.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        error = str(result.get("error", ""))
        files = sorted(item.name for item in ticket.directory.iterdir()
                       if item.is_file() and item.name != "export.json")
        manifest.update(status="failed" if error else "saved", error=error,
                        files=files, finished_at=_iso_now())
        self.manager.repository._write_json(path, manifest)
        event_type = "waveform_save_failed" if error else "waveform_saved"
        message = "数据保存失败" if error else "波形数据已归档"
        details = {"directory": str(ticket.directory.relative_to(
            self.manager.repository.session_dir(ticket.experiment_id))),
            "files": files, "error": error}
        owner = ticket.standalone or self.manager
        active = owner.active_session
        if active is not None and active.experiment_id == ticket.experiment_id:
            owner.record_event(event_type, message, details)
        else:
            # A recording may finish while its export worker is still writing.
            # Always reload that exact session, never attach to a newer one.
            session = self.manager.repository.load(ticket.experiment_id)
            self.manager.repository.append_event(ticket.experiment_id, {
                "timestamp": _iso_now(), "monotonic_s": None,
                "type": event_type, "message": message, "details": details,
            })
            session.event_count += 1
            self.manager.repository.save(session)
        if ticket.standalone is not None:
            if error:
                ticket.standalone.abort(error)
            else:
                ticket.standalone.complete("波形快照已保存（非连续实验记录）")
