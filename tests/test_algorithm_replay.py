import csv
from pathlib import Path

import numpy as np
import pytest

from algorithm_replay import native_module, demo_capture, compare_capture, prepare_capture


@pytest.fixture
def native():
    try:
        return native_module()
    except RuntimeError as exc:
        pytest.skip(str(exc))


def test_replay_eso_matches_simulink_golden_and_rls_training_prefix(native):
    with Path(__file__).with_name("simulink_rls_golden.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    arrays = [np.array([float(r[k]) for r in rows]) for k in ("id_a", "iq_a", "ud_v", "uq_v")]
    result = native.replay(*arrays, 100000, inductance=.01936, exact_reference=True,
                           warmup_s=0, train_fraction=.9)
    indices = np.rint(result["time"]*100000).astype(int)
    for axis in ("d", "q"):
        expected = np.array([float(r[f"i{axis}_hat_a"]) for r in rows])[indices]
        np.testing.assert_allclose(result[f"i{axis}_hat"], expected, atol=2e-12, rtol=0)
        training = indices < int(len(rows)*.9)
        expected_theta = np.array([[float(r[f"theta_{axis}_{j}"]) for j in range(1, 8)] for r in rows])[indices]
        np.testing.assert_allclose(result[f"theta_{axis}"][training], expected_theta[training], atol=3e-5, rtol=0)
        holdout = result[f"theta_{axis}"][~training]
        assert np.all(holdout == holdout[0])


def test_comparison_changes_bandwidth_and_metrics_use_full_data(native):
    columns = demo_capture()
    profiles = [dict(inductance=.00066, bandwidth=b, **{"lambda": 1.}, rls=True) for b in (2000.,4000.)]
    result = compare_capture(columns, profiles)
    a, b = result["results"]
    assert a["evaluated_count"] == 4800
    assert len(a["time"]) <= 5001
    assert not np.allclose(a["iq_hat"], b["iq_hat"])
    # Error computed by C++ at native sample rate, independently reconstructed
    # with a short (non-decimated) input to verify the exact holdout convention.
    arrays = [columns[k][:4000] for k in ("id_a", "iq_a", "ud_v", "uq_v")]
    short = native.replay(*arrays, 16000, warmup_s=0)
    error = arrays[1][2800:]-short["iq_prior"][2800:]
    assert short["metrics"]["prior_rmse_q"] == pytest.approx(np.sqrt(np.mean(error**2)))


def test_rls_disabled_and_input_sequence_gap(native):
    columns = demo_capture()
    data = prepare_capture(columns)
    result = native.replay(*data["arrays"], data["rate"], rls=False)
    assert result["metrics"]["rls_holdout_rmse_q"] is None
    columns["sample_seq"] = np.arange(16000) % 65536
    columns["sample_seq"][100:] += 1
    with pytest.raises(ValueError, match="不连续"):
        prepare_capture(columns)


def test_native_rejects_invalid_parameters(native):
    data = prepare_capture(demo_capture())
    with pytest.raises(ValueError):
        native.replay(*data["arrays"], data["rate"], lambda_=.5)
    with pytest.raises(ValueError):
        native.replay(*data["arrays"], data["rate"], bandwidth=float("nan"))
