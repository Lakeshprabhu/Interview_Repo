"""Standalone validation script for the assessment pipeline outputs.

Run directly with `uv run validate.py` or `make validate`. Exits with code 1
on any failure, code 0 if all checks pass. Importable as a module: callers
get a `ValidationError` instead of a process exit.
"""

import json
import sys
from pathlib import Path


REQUIRED_FILES = [
    "strategies.json",
    "data_manifest.json",
    "metrics.json",
    "critiques.json",
    "report.md",
    "llm_calls.jsonl",
]

REQUIRED_DIR_GLOBS = [
    ("specs", "*.json"),
    ("ledgers", "*.csv"),
]

JSON_FILES_TO_VALIDATE = [
    "strategies.json",
    "data_manifest.json",
    "metrics.json",
    "critiques.json",
]


class ValidationError(Exception):
    pass


def _fail(msg: str):
    raise ValidationError(msg)


def _ok(msg: str):
    print(f"OK: {msg}")


def _check_required_files(root: Path):
    for f in REQUIRED_FILES:
        if not (root / f).exists():
            _fail(f"Missing required file: {f}")
        _ok(f"file exists: {f}")
    for d, pattern in REQUIRED_DIR_GLOBS:
        path = root / d
        if not path.is_dir():
            _fail(f"Missing required directory: {d}")
        if not list(path.glob(pattern)):
            _fail(f"No files matching {d}/{pattern}")
        _ok(f"{d}/{pattern} present")


def _check_json_validity(root: Path):
    for f in JSON_FILES_TO_VALIDATE:
        try:
            json.loads((root / f).read_text(encoding="utf-8"))
        except Exception as e:
            _fail(f"Invalid JSON in {f}: {e}")
        _ok(f"valid JSON: {f}")


def _check_specs_have_ambiguities(root: Path):
    for spec_file in (root / "specs").glob("*.json"):
        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
        except Exception as e:
            _fail(f"Invalid JSON in {spec_file}: {e}")
        amb = spec.get("explicit_ambiguities")
        if not isinstance(amb, list) or len(amb) < 3:
            count = len(amb) if isinstance(amb, list) else "invalid"
            _fail(f"{spec_file.name}: explicit_ambiguities must have >= 3 items (found {count})")
        _ok(f"{spec_file.name}: {len(amb)} explicit_ambiguities")


def _check_strategy_c_high_risk(root: Path):
    critiques = json.loads((root / "critiques.json").read_text(encoding="utf-8"))
    c = critiques.get("C")
    if not c:
        _fail("critiques.json missing entry for strategy C")
    risk = str(c.get("risk_level", "")).lower()
    if risk != "high":
        _fail(f"Strategy C must be flagged as high risk (got risk_level={c.get('risk_level')!r})")
    _ok("Strategy C flagged as high risk")


def _check_audit_log_stages(root: Path):
    stages = set()
    for line in (root / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            stages.add(json.loads(line).get("stage"))
        except json.JSONDecodeError as e:
            _fail(f"Invalid JSONL line in llm_calls.jsonl: {e}")
    for required in ("STRATEGIES_FORMALISED", "STRATEGIES_CRITIQUED"):
        if required not in stages:
            _fail(f"llm_calls.jsonl missing stage: {required}")
        _ok(f"audit log contains stage: {required}")


def validate(root: str = "."):
    root_path = Path(root)
    _check_required_files(root_path)
    _check_json_validity(root_path)
    _check_specs_have_ambiguities(root_path)
    _check_strategy_c_high_risk(root_path)
    _check_audit_log_stages(root_path)
    print("\nALL VALIDATIONS PASSED")


if __name__ == "__main__":
    try:
        validate()
    except ValidationError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
