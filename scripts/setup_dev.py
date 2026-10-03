"""One-command resumable environment setup."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from scripts.checkpoint import Checkpoint  # noqa: E402

_STATE = _ROOT / "data" / ".setup_state.json"


def _say(m: str) -> None:
    print(f"  {m}")


def _header(m: str) -> None:
    print()
    print("=" * 70)
    print(f"  {m}")
    print("=" * 70)


def _run(cmd: str) -> int:
    print(f"\n$ {cmd}")
    print("-" * 70)
    t0 = time.monotonic()
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    print((r.stdout + r.stderr).strip()[-1500:])
    print(f"  [elapsed: {time.monotonic() - t0:.1f}s]")
    print("-" * 70)
    if r.returncode != 0:
        sys.exit(f"FAILED: {cmd}")
    return r.returncode


def _have_prices() -> bool:
    p = _ROOT / "data/raw/prices/yfinance"
    return p.exists() and len(list(p.glob("*/*.parquet"))) >= 30


def _have_macro() -> bool:
    p = _ROOT / "data/raw/macro/fred"
    return p.exists() and len([d for d in p.iterdir() if d.is_dir()]) >= 100


def _have_sec() -> bool:
    p = _ROOT / "data/raw/fundamentals/sec"
    return p.exists() and len([d for d in p.iterdir() if d.is_dir()]) >= 25


def _have_secrets() -> tuple[bool, bool]:
    return (bool(os.environ.get("FRED_API_KEY")), bool(os.environ.get("SEC_USER_AGENT")))


def step_secrets() -> None:
    _header("Secrets")
    fred, sec = _have_secrets()
    if not fred or not sec:
        try:
            from google.colab import userdata  # type: ignore

            if not fred:
                v = userdata.get("FRED_API_KEY") or ""
                if v:
                    os.environ["FRED_API_KEY"] = v
                    fred = True
            if not sec:
                v = userdata.get("SEC_USER_AGENT") or ""
                if v:
                    os.environ["SEC_USER_AGENT"] = v
                    sec = True
        except Exception:
            pass
    _say(f"FRED_API_KEY   : {fred}")
    _say(f"SEC_USER_AGENT : {sec}")


def step_packages(cp: Checkpoint) -> None:
    _header("Python packages")
    if cp.is_done("packages"):
        _say("checkpoint: done — skipping")
        return
    cp.start("packages")
    _run('pip install -q -e ".[dev,dbt,quality]"')
    _run(
        "python -c \"import ruff, mypy, pandera.pandas, dbt, yfinance, yaml; print('imports OK')\""
    )
    cp.done("packages")


def step_dbt_profile(cp: Checkpoint) -> None:
    _header("dbt profile + deps")
    if cp.is_done("dbt_profile"):
        _say("checkpoint: done — skipping")
        return
    cp.start("dbt_profile")
    prof = _ROOT / "dbt/profiles.yml"
    if not prof.exists():
        prof.write_text((_ROOT / "dbt/profiles.example.yml").read_text())
        _say("regenerated dbt/profiles.yml")
    _run("dbt deps --project-dir dbt --profiles-dir dbt")
    cp.done("dbt_profile")


def step_prices(cp: Checkpoint) -> None:
    _header("Prices")
    if _have_prices():
        if not cp.is_done("prices"):
            cp.done("prices", meta={"source": "preexisting"})
        _say("data present — skipping")
        return
    cp.start("prices")
    _run(
        "python -m ingestion.cli --start 2015-01-01 --end 2026-10-02 "
        "--log-format json 2>&1 | tail -1"
    )
    cp.done("prices")


def step_macro(cp: Checkpoint, *, skip: bool = False) -> None:
    _header("Macro")
    if _have_macro():
        if not cp.is_done("macro"):
            cp.done("macro", meta={"source": "preexisting"})
        _say("data present — skipping")
        return
    if skip:
        _say("--skip-macro: not running")
        return
    cp.start("macro")
    _say("will ingest (~2 hours)")
    _run("python -m ingestion.macro.cli --start 2015-01-01 --log-format json")
    cp.done("macro")


def step_sec(cp: Checkpoint) -> None:
    _header("SEC fundamentals")
    if _have_sec():
        if not cp.is_done("sec"):
            cp.done("sec", meta={"source": "preexisting"})
        _say("data present — skipping")
        return
    cp.start("sec")
    _run("python -m ingestion.sec.cli --log-format json 2>&1 | tail -1")
    cp.done("sec")


def step_warehouse(cp: Checkpoint) -> None:
    _header("Warehouse (dbt seed + build)")
    if cp.is_done("warehouse"):
        _say("checkpoint: done — skipping")
        return
    cp.start("warehouse")
    _run("dbt seed --project-dir dbt --profiles-dir dbt")
    _run("dbt build --project-dir dbt --profiles-dir dbt 2>&1 | tail -5")
    cp.done("warehouse")


def report() -> None:
    _header("Environment report")
    fred, sec = _have_secrets()
    _say(f"FRED_API_KEY            : {fred}")
    _say(f"SEC_USER_AGENT          : {sec}")
    _say(f"dbt/profiles.yml        : {(_ROOT / 'dbt/profiles.yml').exists()}")
    _say(f"prices present          : {_have_prices()}")
    _say(f"macro present           : {_have_macro()}")
    _say(f"SEC fundamentals present: {_have_sec()}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="setup_dev.py")
    p.add_argument("--check", action="store_true")
    p.add_argument("--skip-macro", action="store_true")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--reset", action="store_true")
    args = p.parse_args(argv)

    cp = Checkpoint(_STATE)

    if args.status:
        print(cp.summary())
        return 0
    if args.reset:
        cp.reset()
        print(f"  reset: {_STATE}")
        return 0

    print(cp.summary())
    step_secrets()
    if args.check:
        report()
        return 0

    step_packages(cp)
    step_dbt_profile(cp)
    if args.quick:
        report()
        return 0

    step_prices(cp)
    step_macro(cp, skip=args.skip_macro)
    step_sec(cp)
    step_warehouse(cp)

    report()
    print(cp.summary())
    _header("Setup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
