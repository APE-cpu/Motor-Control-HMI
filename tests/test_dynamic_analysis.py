import numpy as np
import pytest

from analysis_dynamic import (series, stft_map, cwt_map, order_map,
                              response_metrics, demo_snapshot)


def test_stft_chirp_ridge_and_psd_integral():
    fs = 4000
    t = np.arange(fs*2)/fs
    y = np.cos(2*np.pi*(50*t+25*t*t))
    result = stft_map(t, y, fs, 512)
    ridge = result["axis"][result["power"].argmax(axis=0)]
    assert np.max(abs(ridge-(50+50*result["time"]))) < 2*result["resolution"]
    assert np.median(np.sum(result["power"], axis=0)*result["resolution"]) == pytest.approx(.5, rel=.015)


def test_cwt_tone_and_masked_edges():
    fs = 1000
    t = np.arange(2000)/fs
    y = np.cos(2*np.pi*100*t)
    result = cwt_map(t, y, fs, 40, 200)
    assert np.all(np.isnan(result["power"][:, 0]))
    center = result["power"][:, 500]
    assert result["axis"][np.nanargmax(center)] == pytest.approx(100, abs=4)


def test_order_removes_speed_sweep_and_accepts_reverse_rotation():
    snapshot, speed = demo_snapshot()
    t, y, fs = series(snapshot)
    result = order_map(t, y, fs, speed)
    power = result["power"].mean(axis=1)
    assert result["axis"][power.argmax()] == pytest.approx(1)
    assert power[np.argmin(abs(result["axis"]-2))] / power.max() == pytest.approx(.25**2, rel=.12)
    reverse = dict(speed, values=-speed["values"])
    backward = order_map(t, y, fs, reverse)
    np.testing.assert_allclose(backward["power"], result["power"])


def test_electrical_angle_order_and_missing_reference_coverage():
    fs = 2000
    t = np.arange(4000)/fs
    cycles = 20*t+10*t*t
    y = np.cos(2*np.pi*3*cycles)
    reference = dict(times=t, values=(cycles*360)%360, sample_rate_hz=fs)
    result = order_map(t, y, fs, reference, "angle", 6)
    assert result["axis"][result["power"].mean(axis=1).argmax()] == pytest.approx(3)
    with pytest.raises(ValueError, match="覆盖"):
        order_map(t, y, fs, dict(reference, times=t[1:], values=reference["values"][1:]))


def test_first_order_step_metrics_rising_and_falling():
    t = np.arange(3000)/1000
    for sign in (1, -1):
        y = sign*np.where(t >= .2, 1-np.exp(-np.maximum(t-.2, 0)/.1), 0)
        result = response_metrics(t, y, .2, sign)
        m = result["metrics"]
        assert m["rise_s"] == pytest.approx(.1*np.log(9), abs=.002)
        assert m["settling_s"] == pytest.approx(-.1*np.log(.02), abs=.002)
        assert m["overshoot_pct"] < .001
        assert m["ringing_hz"] is None
        assert m["saturation_fraction"] is None


def test_unsettled_response_and_explicit_saturation():
    t = np.arange(1000)/1000
    y = np.where(t > .2, .8, 0)
    result = response_metrics(t, y, .2, 1, saturation=t>.6)
    assert result["metrics"]["settling_s"] is None
    assert result["metrics"]["rise_s"] is None
    assert result["metrics"]["saturation_s"] == pytest.approx(.999-.601)
    assert result["metrics"]["saturation_fraction"] == pytest.approx((.999-.601)/(.999-.2))


def test_time_gap_and_nan_are_not_silently_removed():
    t = np.arange(100)/1000
    y = np.sin(t)
    with pytest.raises(ValueError, match="丢点"):
        series(dict(times=np.delete(t, 50), values=np.delete(y, 50)))
    y[4] = np.nan
    with pytest.raises(ValueError, match="无效"):
        series(dict(times=t, values=y))


def test_small_timestamp_jitter_is_resampled():
    t = np.arange(1000)/1000
    jittered = t + 1e-5*np.sin(np.arange(1000))
    uniform, y, fs = series(dict(times=jittered, values=jittered*3))
    np.testing.assert_allclose(np.diff(uniform), 1/fs, atol=1e-12)
    np.testing.assert_allclose(y, uniform*3, atol=1e-12)


def test_irregular_slow_speed_reference_does_not_require_uniform_timestamps():
    t = np.arange(4000)/2000
    ref_time = np.r_[0, np.cumsum(np.tile([.06, .13, .11, .08, .12], 4))]
    ref = dict(times=ref_time, values=np.full(len(ref_time), 1200.))
    y = np.cos(2*np.pi*40*t)
    result = order_map(t, y, 2000, ref, max_order=5)
    assert result["axis"][result["power"].mean(axis=1).argmax()] == pytest.approx(2)


def test_irregular_response_keeps_event_timing_and_time_weighted_saturation():
    t = np.r_[0., np.cumsum(np.tile([.006, .013, .011], 150))]
    y = np.where(t < .2, 0., 1-np.exp(-np.maximum(t-.2,0)/.15))
    raw_t, raw_y, _ = series(dict(times=t, values=y), timing="raw")
    np.testing.assert_array_equal(raw_t, t)
    result = response_metrics(raw_t, raw_y, .2, 1)
    assert result["metrics"]["rise_s"] == pytest.approx(.15*np.log(9), abs=.003)
    assert result["metrics"]["settling_s"] == pytest.approx(-.15*np.log(.02), abs=.02)
    regular_t, _, _ = series(dict(times=t, values=y), timing="resample")
    assert np.ptp(np.diff(regular_t)) < 1e-12


def test_real_interruption_still_reports_gap_location():
    t = np.arange(100)/100
    t[50:] += 1
    with pytest.raises(ValueError, match="中断"):
        series(dict(times=t, values=np.sin(t)), timing="resample")


def test_reversal_and_unsafe_order_range_rejected():
    snapshot, speed = demo_snapshot()
    t, y, fs = series(snapshot)
    with pytest.raises(ValueError, match="最大阶次"):
        order_map(t, y, fs, speed, max_order=100)
    speed["values"][3000:] *= -1
    with pytest.raises(ValueError, match="单向"):
        order_map(t, y, fs, speed)
