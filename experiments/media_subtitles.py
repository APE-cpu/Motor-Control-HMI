"""实验录像字幕：把实验日期、转速、电流和关键事件按时间对齐成字幕。

不改动录像本身（不重新编码、实验时不占 CPU）：
  - <录像名>.srt 放在录像旁边，VLC / PotPlayer / Windows 媒体播放器打开录像时自动加载；
  - WebVTT 文本供实验日志 HTML 的 <video><track> 使用。
时间对齐：录像清单记录了录像开始相对实验开始的秒数 start_offset_s；慢遥测与事件的
monotonic_s 也是相对实验开始，二者同一时间轴。
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
from pathlib import Path

from widgets.media_recorder import read_media_manifest

_EVENT_HOLD_S = 4.0
_SKIP_EVENTS = {"media_recording_started", "media_recording_stopped"}


def _stamp(seconds: float, vtt: bool) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    sep = "." if vtt else ","
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _number(row: dict, key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return None
    return value


def build_cues(session, entry: dict, telemetry: list[dict], events: list[dict],
               step_s: float = 1.0) -> list[tuple[float, float, str]]:
    """返回 (录像内开始秒, 结束秒, 文本) 列表。"""
    duration = float(entry.get("duration_s") or 0.0)
    offset = float(entry.get("start_offset_s") or 0.0)
    if duration <= 0:
        return []
    try:
        started = datetime.fromisoformat(session.started_at)
    except (TypeError, ValueError):
        started = None
    rows = sorted((r for r in telemetry if _number(r, "monotonic_s") is not None),
                  key=lambda r: _number(r, "monotonic_s"))
    marks = [(float(e["monotonic_s"]), str(e.get("message") or e.get("type") or ""))
             for e in events
             if e.get("monotonic_s") is not None and e.get("type") not in _SKIP_EVENTS]
    cues = []
    t = 0.0
    index = 0
    while t < duration:
        end = min(duration, t + step_s)
        session_t = offset + t
        while index + 1 < len(rows) and _number(rows[index + 1], "monotonic_s") <= session_t:
            index += 1
        lines = []
        clock = (started + timedelta(seconds=session_t)).strftime("%Y-%m-%d %H:%M:%S") \
            if started else ""
        lines.append(f"{clock}  {session.name}  T+{session_t:.0f} s".strip())
        if rows and _number(rows[index], "monotonic_s") <= session_t + step_s:
            row = rows[index]
            speed, target = _number(row, "speed_actual"), _number(row, "speed_target")
            current = _number(row, "current_actual")
            parts = []
            if speed is not None:
                parts.append(f"转速 {speed:.0f} rpm" +
                             (f"（给定 {target:.0f}）" if target is not None else ""))
            if current is not None:
                parts.append(f"Iq {current:.2f} A")
            if parts:
                lines.append(" · ".join(parts))
        active = [text for when, text in marks if when <= session_t + step_s
                  and session_t < when + _EVENT_HOLD_S and text]
        if active:
            lines.append("▶ " + "；".join(active[-2:]))
        cues.append((t, end, "\n".join(lines)))
        t = end
    return cues


def to_srt(cues) -> str:
    return "\n".join(f"{i}\n{_stamp(a, False)} --> {_stamp(b, False)}\n{text}\n"
                     for i, (a, b, text) in enumerate(cues, 1))


def to_vtt(cues) -> str:
    return "WEBVTT\n\n" + "\n".join(f"{_stamp(a, True)} --> {_stamp(b, True)}\n{text}\n"
                                    for a, b, text in cues)


def vtt_data_uri(cues) -> str:
    return "data:text/vtt;base64," + base64.b64encode(to_vtt(cues).encode("utf-8")).decode("ascii")


def write_session_subtitles(repository, experiment_id: str) -> list[Path]:
    """为实验的每段录像写 .srt 字幕（放在录像旁边），返回写出的文件。"""
    session = repository.load(experiment_id)
    media_dir = repository.session_dir(experiment_id) / "media"
    manifest = read_media_manifest(media_dir)
    if not manifest:
        return []
    telemetry = repository.read_telemetry(experiment_id)
    events = repository.read_events(experiment_id)
    written = []
    for entry in manifest:
        video = media_dir / str(entry.get("file"))
        if not video.exists():
            continue
        cues = build_cues(session, entry, telemetry, events)
        if cues:
            path = video.with_suffix(".srt")
            path.write_text(to_srt(cues), encoding="utf-8-sig")
            written.append(path)
    return written
