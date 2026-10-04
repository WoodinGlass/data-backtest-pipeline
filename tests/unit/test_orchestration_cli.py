"""Unit tests for orchestration.cli. ADR 0016."""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestration.cli import (
    _parse_kwargs,
    build_parser,
    cmd_info,
    cmd_list,
    cmd_run,
    cmd_schedule,
)


def _ns(**kwargs):
    """Minimal namespace for cmd functions."""
    return type("N", (), kwargs)()


# =====================================================================
# Parser
# =====================================================================


class TestParser:
    def test_prog_name(self) -> None:
        assert build_parser().prog == "dbp-orchestrate"

    def test_subcommands(self) -> None:
        p = build_parser()
        sub = next(
            a
            for a in p._actions
            if getattr(a, "dest", None) == "command"
            and isinstance(getattr(a, "choices", None), dict)
        )
        assert sorted(sub.choices.keys()) == ["info", "list", "run", "schedule"]

    def test_run_args(self) -> None:
        args = build_parser().parse_args(["run", "daily_refresh", "--arg", "start=2024-01-01"])
        assert args.flow == "daily_refresh"
        assert args.arg == ["start=2024-01-01"]
        assert args.dry_run is False

    def test_dry_run_flag(self) -> None:
        args = build_parser().parse_args(["run", "x", "--dry-run"])
        assert args.dry_run is True


# =====================================================================
# _parse_kwargs
# =====================================================================


class TestParseKwargs:
    def test_types(self) -> None:
        out = _parse_kwargs(
            [
                "s=hello",
                "n=42",
                "f=0.75",
                "t=true",
                "off=false",
                "empty=none",
                'j={"a":1}',
                "l=[1,2]",
            ]
        )
        assert out["s"] == "hello"
        assert out["n"] == 42
        assert out["f"] == 0.75
        assert out["t"] is True
        assert out["off"] is False
        assert out["empty"] is None
        assert out["j"] == {"a": 1}
        assert out["l"] == [1, 2]

    def test_missing_equals_rejected(self) -> None:
        import pytest

        with pytest.raises(SystemExit):
            _parse_kwargs(["noequals"])


# =====================================================================
# cmd_list
# =====================================================================


class TestCmdList:
    def test_prints_all_flows(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_list(None)
        out = buf.getvalue()
        assert rc == 0
        for name in ("ingest_prices", "dbt_build", "daily_refresh", "full_refresh"):
            assert name in out


# =====================================================================
# cmd_info
# =====================================================================


class TestCmdInfo:
    def test_composite(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_info(_ns(flow="weekly_refresh"))
        assert rc == 0
        assert "composite" in buf.getvalue()

    def test_stage(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_info(_ns(flow="ingest_prices"))
        assert rc == 0
        assert "stage" in buf.getvalue()

    def test_unknown(self) -> None:
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            rc = cmd_info(_ns(flow="nonexistent"))
        assert rc == 2


# =====================================================================
# cmd_schedule
# =====================================================================


class TestCmdSchedule:
    def test_shows_both_crons(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_schedule(None)
        out = buf.getvalue()
        assert rc == 0
        assert "0 22 * * 1-5" in out
        assert "0 23 * * 0" in out


# =====================================================================
# cmd_run
# =====================================================================


class TestCmdRun:
    def test_dry_run(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_run(
                _ns(
                    flow="daily_refresh",
                    arg=["start=2024-01-01"],
                    dry_run=True,
                    json_out=None,
                )
            )
        assert rc == 0
        assert "[dry-run]" in buf.getvalue()

    def test_runs_flow(self) -> None:
        called: dict = {}

        def fake(**kwargs):
            called.update(kwargs)
            return {
                "flow": "daily_refresh",
                "steps": {
                    "prices": {"returncode": 0},
                },
            }

        with patch("orchestration.cli._load_flow", return_value=fake):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_run(
                    _ns(
                        flow="daily_refresh",
                        arg=["start=2024-01-01"],
                        dry_run=False,
                        json_out=None,
                    )
                )
        assert rc == 0
        assert called["start"] == "2024-01-01"
        assert "settings" in called
        assert "[OK] prices" in buf.getvalue()

    def test_unknown_flow(self) -> None:
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            rc = cmd_run(
                _ns(
                    flow="totally_unknown",
                    arg=[],
                    dry_run=False,
                    json_out=None,
                )
            )
        assert rc == 2

    def test_failure(self) -> None:
        def failing(**kwargs):
            raise RuntimeError("exploded")

        with patch("orchestration.cli._load_flow", return_value=failing):
            buf = io.StringIO()
            buf_err = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf_err):
                rc = cmd_run(
                    _ns(
                        flow="daily_refresh",
                        arg=[],
                        dry_run=False,
                        json_out=None,
                    )
                )
        assert rc == 1
        assert "exploded" in buf_err.getvalue()

    def test_json_out(self) -> None:
        def fake(**kwargs):
            return {"flow": "daily_refresh", "steps": {}}

        with tempfile.TemporaryDirectory() as td:
            outfile = Path(td) / "result.json"
            with patch("orchestration.cli._load_flow", return_value=fake):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_run(
                        _ns(
                            flow="daily_refresh",
                            arg=[],
                            dry_run=False,
                            json_out=str(outfile),
                        )
                    )
            assert rc == 0
            data = json.loads(outfile.read_text())
            assert data["flow"] == "daily_refresh"
