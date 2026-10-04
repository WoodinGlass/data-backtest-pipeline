"""Unit tests for orchestration.flows. ADR 0016 section 4-5."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from orchestration._subprocess import CommandResult
from orchestration.config import OrchestrationSettings
from orchestration.flows import backtest, composite, features, ingest, quality, warehouse


def _unwrap(obj):
    return getattr(obj, "fn", obj)


def _capture(cap):
    def _run(cmd, cwd=None, env=None, *, check=True, **kw):
        cap["cmd"] = cmd
        cap["cwd"] = cwd
        cap["env"] = env
        return CommandResult(0, "OK\n", "", 1.0, cmd)

    return _run


def _fail(cap):
    def _run(cmd, cwd=None, env=None, *, check=True, **kw):
        raise subprocess.CalledProcessError(2, cmd, output="", stderr="kaboom")

    return _run


# =====================================================================
# ingest flows
# =====================================================================


class TestIngestFlows:
    def test_prices_default_args(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.ingest.run_command", side_effect=_capture(cap)):
            _unwrap(ingest.ingest_prices)(settings=OrchestrationSettings())
        assert cap["cmd"][1:3] == ["-m", "ingestion.cli"]
        assert "--start" not in cap["cmd"]
        assert "--end" not in cap["cmd"]

    def test_prices_with_dates(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.ingest.run_command", side_effect=_capture(cap)):
            _unwrap(ingest.ingest_prices)(
                start="2024-01-01",
                end="2024-01-31",
                settings=OrchestrationSettings(),
            )
        assert "--start" in cap["cmd"]
        assert "2024-01-01" in cap["cmd"]
        assert "--end" in cap["cmd"]
        assert "2024-01-31" in cap["cmd"]

    def test_macro_with_mode(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.ingest.run_command", side_effect=_capture(cap)):
            _unwrap(ingest.ingest_macro)(mode="latest", settings=OrchestrationSettings())
        assert cap["cmd"][1:3] == ["-m", "ingestion.macro.cli"]
        assert "--mode" in cap["cmd"]
        assert "latest" in cap["cmd"]

    def test_macro_no_mode(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.ingest.run_command", side_effect=_capture(cap)):
            _unwrap(ingest.ingest_macro)(settings=OrchestrationSettings())
        assert "--mode" not in cap["cmd"]

    def test_sec(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.ingest.run_command", side_effect=_capture(cap)):
            _unwrap(ingest.ingest_sec)(settings=OrchestrationSettings())
        assert cap["cmd"][1:3] == ["-m", "ingestion.sec.cli"]

    def test_prices_propagates_failure(self) -> None:
        with (
            patch("orchestration.flows.ingest.run_command", side_effect=_fail({})),
            pytest.raises(subprocess.CalledProcessError),
        ):
            _unwrap(ingest.ingest_prices)(settings=OrchestrationSettings())


# =====================================================================
# warehouse flow
# =====================================================================


class TestWarehouseFlow:
    def test_base_command(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.warehouse.run_command", side_effect=_capture(cap)):
            _unwrap(warehouse.dbt_build)(settings=OrchestrationSettings())
        assert cap["cmd"][0] == "dbt"
        assert cap["cmd"][1] == "build"
        assert "--project-dir" in cap["cmd"]
        assert "--profiles-dir" in cap["cmd"]
        assert "--select" not in cap["cmd"]
        assert "--vars" not in cap["cmd"]

    def test_with_selector_and_vars(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.warehouse.run_command", side_effect=_capture(cap)):
            _unwrap(warehouse.dbt_build)(
                selector="+fct_prices_daily",
                vars_json='{"x": 1}',
                settings=OrchestrationSettings(),
            )
        assert "--select" in cap["cmd"]
        assert "+fct_prices_daily" in cap["cmd"]
        assert "--vars" in cap["cmd"]

    def test_propagates_failure(self) -> None:
        with (
            patch("orchestration.flows.warehouse.run_command", side_effect=_fail({})),
            pytest.raises(subprocess.CalledProcessError),
        ):
            _unwrap(warehouse.dbt_build)(settings=OrchestrationSettings())


# =====================================================================
# quality flow
# =====================================================================


class TestQualityFlow:
    def test_defaults(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.quality.run_command", side_effect=_capture(cap)):
            _unwrap(quality.quality_gate)(settings=OrchestrationSettings())
        assert cap["cmd"][1:3] == ["-m", "quality.cli"]
        assert "--tickers-from-raw" in cap["cmd"]

    def test_with_json_and_skips(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.quality.run_command", side_effect=_capture(cap)):
            _unwrap(quality.quality_gate)(
                tickers_from_raw=False,
                json_report="reports/q.json",
                skip=["sec", "stg_sec"],
                settings=OrchestrationSettings(),
            )
        assert "--tickers-from-raw" not in cap["cmd"]
        assert "--json" in cap["cmd"]
        assert "reports/q.json" in cap["cmd"]
        assert cap["cmd"].count("--skip") == 2

    def test_propagates_failure(self) -> None:
        with (
            patch("orchestration.flows.quality.run_command", side_effect=_fail({})),
            pytest.raises(subprocess.CalledProcessError),
        ):
            _unwrap(quality.quality_gate)(settings=OrchestrationSettings())


# =====================================================================
# features flow
# =====================================================================


class TestFeaturesFlow:
    def test_defaults(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.features.run_command", side_effect=_capture(cap)):
            _unwrap(features.build_features)(settings=OrchestrationSettings())
        assert cap["cmd"][1:3] == ["-m", "features.cli"]
        assert "--info" not in cap["cmd"]

    def test_info_only(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.features.run_command", side_effect=_capture(cap)):
            _unwrap(features.build_features)(info_only=True, settings=OrchestrationSettings())
        assert "--info" in cap["cmd"]


# =====================================================================
# backtest flow
# =====================================================================


class TestBacktestFlow:
    def test_defaults_mlflow_on(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.backtest.run_command", side_effect=_capture(cap)):
            _unwrap(backtest.run_backtest)(settings=OrchestrationSettings())
        assert cap["cmd"][1] == "scripts/run_backtest.py"
        assert "--mlflow" in cap["cmd"]
        assert "--no-register-model" not in cap["cmd"]
        assert "--tracking-uri" not in cap["cmd"]

    def test_mlflow_off(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.backtest.run_command", side_effect=_capture(cap)):
            _unwrap(backtest.run_backtest)(mlflow=False, settings=OrchestrationSettings())
        assert "--mlflow" not in cap["cmd"]

    def test_tracking_uri(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.backtest.run_command", side_effect=_capture(cap)):
            _unwrap(backtest.run_backtest)(
                tracking_uri="sqlite:////tmp/foo.db",
                settings=OrchestrationSettings(),
            )
        assert "--tracking-uri" in cap["cmd"]
        assert "sqlite:////tmp/foo.db" in cap["cmd"]

    def test_no_register(self) -> None:
        cap: dict = {}
        with patch("orchestration.flows.backtest.run_command", side_effect=_capture(cap)):
            _unwrap(backtest.run_backtest)(
                register_model=False,
                settings=OrchestrationSettings(),
            )
        assert "--no-register-model" in cap["cmd"]

    def test_propagates_failure(self) -> None:
        with (
            patch("orchestration.flows.backtest.run_command", side_effect=_fail({})),
            pytest.raises(subprocess.CalledProcessError),
        ):
            _unwrap(backtest.run_backtest)(settings=OrchestrationSettings())


# =====================================================================
# composite flows
# =====================================================================


def _recorder(calls):
    def _mk(name):
        def _fn(*args, **kwargs):
            calls.append(name)
            return {"step": name, "ok": True}

        return _fn

    return _mk


class TestCompositeFlows:
    def test_daily_sequence(self) -> None:
        calls: list[str] = []
        mk = _recorder(calls)
        with (
            patch.object(composite, "ingest_prices", mk("ingest_prices")),
            patch.object(composite, "dbt_build", mk("dbt_build")),
            patch.object(composite, "quality_gate", mk("quality_gate")),
            patch.object(composite, "build_features", mk("build_features")),
        ):
            r = _unwrap(composite.daily_refresh)(settings=OrchestrationSettings())
        assert calls == ["ingest_prices", "dbt_build", "quality_gate", "build_features"]
        assert r["flow"] == "daily_refresh"
        assert set(r["steps"]) == {"prices", "dbt", "quality", "features"}

    def test_weekly_sequence(self) -> None:
        calls: list[str] = []
        mk = _recorder(calls)
        with (
            patch.object(composite, "ingest_macro", mk("ingest_macro")),
            patch.object(composite, "ingest_sec", mk("ingest_sec")),
            patch.object(composite, "dbt_build", mk("dbt_build")),
            patch.object(composite, "quality_gate", mk("quality_gate")),
            patch.object(composite, "build_features", mk("build_features")),
            patch.object(composite, "run_backtest", mk("run_backtest")),
        ):
            r = _unwrap(composite.weekly_refresh)(settings=OrchestrationSettings())
        assert calls == [
            "ingest_macro",
            "ingest_sec",
            "dbt_build",
            "quality_gate",
            "build_features",
            "run_backtest",
        ]
        assert r["flow"] == "weekly_refresh"

    def test_full_sequence(self) -> None:
        calls: list[str] = []
        mk = _recorder(calls)
        with (
            patch.object(composite, "ingest_prices", mk("ingest_prices")),
            patch.object(composite, "ingest_macro", mk("ingest_macro")),
            patch.object(composite, "ingest_sec", mk("ingest_sec")),
            patch.object(composite, "dbt_build", mk("dbt_build")),
            patch.object(composite, "quality_gate", mk("quality_gate")),
            patch.object(composite, "build_features", mk("build_features")),
            patch.object(composite, "run_backtest", mk("run_backtest")),
        ):
            r = _unwrap(composite.full_refresh)(settings=OrchestrationSettings())
        assert calls == [
            "ingest_prices",
            "ingest_macro",
            "ingest_sec",
            "dbt_build",
            "quality_gate",
            "build_features",
            "run_backtest",
        ]
        assert r["flow"] == "full_refresh"
        assert set(r["steps"]) == {
            "prices",
            "macro",
            "sec",
            "dbt",
            "quality",
            "features",
            "backtest",
        }

    def test_composite_propagates_failure(self) -> None:
        def _boom(*a, **kw):
            raise RuntimeError("dbt exploded")

        with (
            patch.object(composite, "ingest_prices", _recorder([])("ingest_prices")),
            patch.object(composite, "dbt_build", _boom),
            pytest.raises(RuntimeError, match="dbt exploded"),
        ):
            _unwrap(composite.daily_refresh)(settings=OrchestrationSettings())
