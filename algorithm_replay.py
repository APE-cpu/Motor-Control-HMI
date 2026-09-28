"""Synchronized data adapter for the independent offline C++ replay module."""
from __future__ import annotations

import csv
import importlib
import hashlib
import sys
from pathlib import Path

import numpy as np


def native_module():
    runtime = Path(__file__).resolve().parent / "native_replay_runtime"
    if runtime.is_dir() and str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    try:
        return importlib.import_module("motor_replay_cpp")
    except ImportError as exc:
        raise RuntimeError("离线 C++ 核未编译；请运行 tools/build_replay.ps1") from exc


def prepare_capture(columns):
    from rls_offline import resolve_voltage_columns_si
    rate = int(columns["rate_hz"])
    q = np.asarray(columns["iq_a"], dtype=float)
    n = len(q)
    if rate <= 0 or not 32 <= n <= 2_000_000:
        raise ValueError("同步数据需要 32～2,000,000 点以及有效采样率")
    def column(name):
        data = np.asarray(columns[name], dtype=float)
        if data.shape != q.shape or not np.all(np.isfinite(data)):
            raise ValueError(name+" 含无效值或未与 Iq 对齐")
        return data
    if "sample_seq" in columns:
        seq = column("sample_seq").astype(np.int64)
        if np.any(np.diff(seq) % 65536 != 1):
            raise ValueError("同步 F1 样本序号不连续，请选用无丢帧记录")
        continuity = "F1 样本序号逐点连续"
    else:
        continuity = "无样本序号；只能校验时间轴，无法排除亚毫秒丢帧"
    if "sample_index" in columns and np.any(np.diff(column("sample_index")) != 1):
        raise ValueError("CSV 样本索引不连续")
    if "time_s" in columns:
        t = column("time_s")
        if np.any(abs(np.diff(t)-1/rate) > .1/rate):
            raise ValueError("采样时间与采样率不一致")
    elif "tick_ms" in columns:
        ticks = column("tick_ms").astype(np.int64)
        increments = np.diff(ticks) % (2**32)
        elapsed = np.r_[0, np.cumsum(increments)]
        if np.max(abs(elapsed-np.arange(n)*1000/rate)) > 2.1:
            raise ValueError("毫秒时间戳存在中断或采样率不匹配")
    if bool(columns.get("id_source_direct", "id_a" in columns)):
        d = column("id_a")
        current_source = "实测 dq 电流"
    else:
        angle = np.deg2rad(column("angle_deg"))
        ia, ib = column("ia_a"), column("ib_a")
        d = ia*np.sin(angle) - (ia+2*ib)/np.sqrt(3)*np.cos(angle)
        current_source = "Id 按 MCSDK Park 约定由 Ia/Ib 重建；Iq 为记录反馈"
    if "ud_v" in columns and "uq_v" in columns:
        u, v = column("ud_v"), column("uq_v")
        voltage_source = "CSV 声明的 SI dq 电压"
    else:
        column("vbus_v")
        for key in (("vd_applied_v", "vq_applied_v") if columns.get("applied_voltage_source_direct")
                    else ("vd_raw", "vq_raw")):
            column(key)
        u, v, voltage_source = resolve_voltage_columns_si(columns)
    arrays = [np.ascontiguousarray(a, dtype=np.float64) for a in (d, q, u, v)]
    if any(not np.all(np.isfinite(a)) for a in arrays):
        raise ValueError("输入含 NaN/Inf")
    return dict(arrays=arrays, rate=rate, voltage_source=voltage_source,
                current_source=current_source, continuity=continuity)


def read_capture(path):
    with open(path, encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or ())
        if not {"id_a", "iq_a", "ud_v", "uq_v", "rate_hz"} <= fields:
            from rls_offline import load_rls_capture_csv
            return load_rls_capture_csv(path)
        names = ["id_a", "iq_a", "ud_v", "uq_v"] + [
            k for k in ("time_s", "sample_seq", "sample_index") if k in fields]
        columns = {k: [] for k in names}
        rate = None
        for i, row in enumerate(reader):
            if i >= 2_000_000:
                raise ValueError("CSV 超过 2,000,000 点，请截取需要验证的片段")
            current_rate = float(row["rate_hz"])
            if current_rate <= 0 or current_rate != int(current_rate) or (rate is not None and current_rate != rate):
                raise ValueError("rate_hz 必须为固定正整数")
            rate = int(current_rate)
            for key in names:
                columns[key].append(float(row[key]))
    return dict(columns, rate_hz=rate or 0)


def compare_capture(columns, profiles, train_fraction=.7, warmup_s=.02,
                    cancel=lambda: False):
    data = prepare_capture(columns)
    digest = hashlib.sha256(str(data["rate"]).encode("ascii"))
    for values in data["arrays"]:
        digest.update(memoryview(values))
    native = native_module()
    results = []
    for profile in profiles:
        if cancel():
            raise InterruptedError("算法验证已取消")
        results.append(native.replay(*data["arrays"], data["rate"],
                       inductance=profile["inductance"], bandwidth=profile["bandwidth"],
                       lambda_=profile["lambda"], rls=profile["rls"],
                       train_fraction=train_fraction, warmup_s=warmup_s))
    # Only plot bounded native traces; all errors above were evaluated at full rate.
    indices = np.rint(results[0]["time"]*data["rate"]).astype(int)
    return dict(results=results, id=data["arrays"][0][indices], iq=data["arrays"][1][indices],
                rate=data["rate"], voltage_source=data["voltage_source"],
                current_source=data["current_source"], continuity=data["continuity"],
                profiles=profiles, train_fraction=train_fraction, warmup_s=warmup_s,
                input_sha256=digest.hexdigest(),
                implementation="motor_replay_cpp v1 · ESO + ARX(3,2-input)")


def demo_capture():
    from scipy.signal import lfilter
    rate = 16000
    t = np.arange(rate)/rate
    u = np.sin(2*np.pi*73*t)+.4*np.sin(2*np.pi*211*t)
    v = 1+np.sin(2*np.pi*47*t)+.3*np.sin(2*np.pi*167*t)
    a = np.exp(-.59/(rate*.00066))
    b = (1-a)/.59
    rng = np.random.default_rng(42)
    # RL synthetic plant, one input delay, independent additive sensor noise.
    d = lfilter([0, b], [1, -a], u)+rng.normal(0, .025, len(t))
    q = lfilter([0, b], [1, -a], v)+rng.normal(0, .025, len(t))
    return dict(id_a=d, iq_a=q, ud_v=u, uq_v=v, rate_hz=rate, time_s=t)
