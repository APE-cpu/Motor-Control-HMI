#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include "motor_core/online_rls.hpp"
#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <vector>

namespace py = pybind11;
using Array = py::array_t<double, py::array::c_style | py::array::forcecast>;

py::dict replay(Array id, Array iq, Array ud, Array uq, unsigned rate,
                double inductance, double bandwidth, double lambda,
                bool rls, double train_fraction, double warmup_s,
                bool exact_reference) {
    const auto n = id.size();
    if (id.ndim()!=1 || iq.ndim()!=1 || ud.ndim()!=1 || uq.ndim()!=1 ||
        iq.size()!=n || ud.size()!=n || uq.size()!=n || n<32 || n>2000000 ||
        rate==0 || !std::isfinite(inductance) || inductance<=0 ||
        !std::isfinite(bandwidth) || bandwidth<=0 || bandwidth>rate*2.0 ||
        !std::isfinite(lambda) || lambda<0.9 || lambda>1.0 ||
        !std::isfinite(train_fraction) || train_fraction<.1 || train_fraction>.9 ||
        !std::isfinite(warmup_s) || warmup_s<0 || warmup_s*rate>=n*train_fraction)
        throw std::invalid_argument("Invalid replay arrays, sample rate or parameters");
    const auto *d=id.data(), *q=iq.data(), *u=ud.data(), *v=uq.data();
    const auto split=static_cast<py::ssize_t>(n*train_fraction);
    const auto warm=static_cast<py::ssize_t>(warmup_s*rate);
    const auto stride=std::max<py::ssize_t>(1, (n+4998)/4999);
    std::vector<double> time, dh, qh, dp, qp, dr, qr;
    std::vector<std::array<double,7>> td, tq;
    double pred_d=0, pred_q=0, corr_d=0, corr_q=0, rls_d=0, rls_q=0;
    motor_core::RlsResult out{};
    {
        py::gil_scoped_release release;
        motor_core::OnlineRlsEstimator estimator(inductance, exact_reference, bandwidth, lambda, rls);
        estimator.set_enabled(true);
        if (warm > 0) estimator.set_rls_adaptation(false);
        for (py::ssize_t i=0; i<n; ++i) {
            if (!std::isfinite(d[i]) || !std::isfinite(q[i]) ||
                !std::isfinite(u[i]) || !std::isfinite(v[i]))
                throw std::invalid_argument("Replay contains non-finite samples");
            if (i==warm) estimator.set_rls_adaptation(true);
            if (i==split) estimator.set_rls_adaptation(false);
            motor_core::RlsDqInput input{static_cast<unsigned>(i*1000/rate), rate,
                                         d[i],q[i],u[i],v[i]};
            if (!estimator.ingest_dq(input,out,true) || !std::isfinite(out.id_hat_a) ||
                !std::isfinite(out.iq_hat_a))
                throw std::runtime_error("Estimator diverged: reduce forgetting/bandwidth or check excitation");
            if(i>=std::max(split,warm)) {
                pred_d+=std::pow(d[i]-out.id_prediction_a,2);
                pred_q+=std::pow(q[i]-out.iq_prediction_a,2);
                corr_d+=std::pow(d[i]-out.id_hat_a,2);
                corr_q+=std::pow(q[i]-out.iq_hat_a,2);
                rls_d+=out.innovation_d*out.innovation_d;
                rls_q+=out.innovation_q*out.innovation_q;
            }
            if(i%stride==0 || i==n-1) {
                time.push_back(static_cast<double>(i)/rate);
                dh.push_back(out.id_hat_a); qh.push_back(out.iq_hat_a);
                dp.push_back(out.id_prediction_a); qp.push_back(out.iq_prediction_a);
                dr.push_back(out.innovation_d); qr.push_back(out.innovation_q);
                td.push_back(out.theta_d); tq.push_back(out.theta_q);
            }
        }
    }
    auto array=[](const std::vector<double>& values) {
        py::array_t<double> result(values.size());
        std::copy(values.begin(),values.end(),result.mutable_data()); return result;
    };
    auto matrix=[](const std::vector<std::array<double,7>>& values) {
        py::array_t<double> result({static_cast<py::ssize_t>(values.size()),py::ssize_t(7)});
        for(size_t i=0;i<values.size();++i)
            std::copy(values[i].begin(),values[i].end(),result.mutable_data()+i*7);
        return result;
    };
    py::dict result;
    result["time"]=array(time); result["id_hat"]=array(dh); result["iq_hat"]=array(qh);
    result["id_prior"]=array(dp); result["iq_prior"]=array(qp);
    result["innovation_d"]=array(dr); result["innovation_q"]=array(qr);
    result["theta_d"]=matrix(td); result["theta_q"]=matrix(tq);
    const auto evaluated=static_cast<double>(n-std::max(split,warm));
    py::dict metrics;
    metrics["prior_rmse_d"]=std::sqrt(pred_d/evaluated);
    metrics["prior_rmse_q"]=std::sqrt(pred_q/evaluated);
    metrics["corrected_rmse_d"]=std::sqrt(corr_d/evaluated);
    metrics["corrected_rmse_q"]=std::sqrt(corr_q/evaluated);
    metrics["rls_holdout_rmse_d"]=rls ? py::cast(std::sqrt(rls_d/evaluated)) : py::none();
    metrics["rls_holdout_rmse_q"]=rls ? py::cast(std::sqrt(rls_q/evaluated)) : py::none();
    result["metrics"]=metrics; result["split_s"]=static_cast<double>(split)/rate;
    result["sample_count"]=n; result["evaluated_count"]=static_cast<long long>(evaluated);
    result["trace_stride"]=stride;
    return result;
}

PYBIND11_MODULE(motor_replay_cpp,m) {
    m.attr("schema_version")=1;
    m.def("replay", &replay, py::arg("id"),py::arg("iq"),py::arg("ud"),py::arg("uq"),
          py::arg("rate"),py::arg("inductance")=.00066,py::arg("bandwidth")=4000.,
          py::arg("lambda_")=1.,py::arg("rls")=true,py::arg("train_fraction")=.7,
          py::arg("warmup_s")=.02,py::arg("exact_reference")=false);
}
