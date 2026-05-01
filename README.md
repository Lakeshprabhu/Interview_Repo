# Trading Strategy Validation Pipeline

A replayable pipeline that converts informal retail-style trading strategy descriptions into strict executable strategy specifications, runs deterministic backtests against historical or simulated OHLCV data, computes risk-adjusted performance metrics, and produces a robustness critique with responsible warnings.

> **⚠️ DISCLAIMER**: This is analysis tooling only and must not be presented as financial advice. Past performance does not guarantee future results.

## Overview

This pipeline implements a multi-stage validation workflow for trading strategies:

1. **Data Acquisition**: Fetches historical data from yfinance (EUR/USD, QQQ) and generates synthetic GBM data for martingale strategies
2. **Strategy Formalisation**: Uses LLM to convert informal strategy descriptions into strict JSON specifications
3. **Deterministic Backtesting**: Pure Python/pandas backtest engine (no LLM math)
4. **Performance Metrics**: Computes Sharpe, Sortino, drawdown, win rate, and more
5. **Strategy Critique**: LLM-based risk assessment with martingale warnings
6. **Robustness Testing**: Walk-forward validation and parameter sensitivity analysis

## Pipeline Stages

```
INIT
 -> STRATEGIES_LOADED
 -> DATA_FETCHED_OR_SIMULATED
 -> STRATEGIES_FORMALISED
 -> SPECS_VALIDATED
 -> BACKTESTS_EXECUTED
 -> LEDGERS_WRITTEN
 -> METRICS_COMPUTED
 -> STRATEGIES_CRITIQUED
 -> OPTIONAL_ROBUSTNESS_TESTS_COMPLETE
 -> REPORT_GENERATED
 -> VALIDATION_COMPLETE
 -> RESULTS_FINALISED
```

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management
- OpenAI API key

### Setup

```bash
# Install dependencies
uv sync

# Set your OpenAI API key
export OPENAI_API_KEY="your-key-here"

# Run the full pipeline
make run
```

### Validate Outputs

```bash
make validate
```

### Clean Generated Artifacts

```bash
make clean
```

## Input Files

### strategies.json

Informal strategy descriptions in JSON format:

```json
[
  {
    "id": "A",
    "name": "London Breakout EUR/USD",
    "description": "I trade EURUSD on the London open..."
  },
  {
    "id": "B",
    "name": "RSI mean reversion on US tech",
    "description": "Watch QQQ on 15min..."
  },
  {
    "id": "C",
    "name": "Synthetic Index Martingale",
    "description": "On Volatility 75 Index 1min..."
  }
]
```

The evaluator may replace this file with equivalent strategy descriptions. The pipeline handles both dictionary and list formats.

## Data Sources

| Strategy | Source | Details |
|----------|--------|---------|
| A (EUR/USD) | yfinance | `EURUSD=X`, 1y daily |
| B (QQQ) | yfinance | `QQQ`, 1y daily |
| C (Synthetic) | GBM simulation | σ=0.75/√year, drift=0, seed=123 |

Synthetic data parameters are documented in `data_manifest.json` for full reproducibility.

## Generated Artifacts

### Required Outputs

| File | Description |
|------|-------------|
| `data_manifest.json` | Documents all data sources and GBM parameters |
| `specs/{strategy_id}.json` | Formal JSON specifications from LLM |
| `ledgers/{strategy_id}.csv` | Per-trade backtest results |
| `metrics.json` | Computed performance metrics |
| `critiques.json` | LLM-generated risk assessments |
| `report.md` | Human-readable summary report |
| `llm_calls.jsonl` | Audit log of all LLM interactions |

### Optional Robustness Outputs

| File | Description |
|------|-------------|
| `walk_forward.json` | 3-window stability analysis |
| `parameter_sensitivity.json` | Parameter sweep results + LLM interpretation |

## Backtest Engine Features

The deterministic backtest engine (`main.py`) handles:

- **Stop-loss and take-profit** logic with intrabar resolution
- **Intrabar assumption**: If both SL and TP touched in same bar, SL hits first
- **Session filters**: Day-of-week exclusions (e.g., skip Wednesdays)
- **End-of-day exits**: Positions closed at session end if neither SL/TP hit
- **Position sizing**: Fixed size, partial entries, and martingale escalation
- **Risk controls**: Max drawdown stops, max trade count limits

### Strategy Implementations

**Strategy A (London Breakout)**: Long on break of previous day's high + 5 pips. SL at previous low. TP at 1.5× risk. Skips Wednesdays. Daily data approximation.

**Strategy B (RSI Mean Reversion)**: Half position when RSI(14) < 25, full position when RSI < 20. Exit when RSI ≥ 50. Long-only.

**Strategy C (Martingale)**: Binary prediction with stake doubling on loss, reset on win. Session stops at $200 drawdown or 50 trades.

## Performance Metrics

Computed deterministically for each strategy:

- Total return
- Win rate
- Profit factor
- Maximum drawdown
- Annualized Sharpe ratio
- Sortino ratio
- Average trade duration
- Exposure percentage
- Number of trades
- Largest losing streak

## LLM Audit Logging

Every LLM call is logged to `llm_calls.jsonl` with:

```json
{
  "stage": "STRATEGIES_FORMALISED | STRATEGIES_CRITIQUED | OPTIONAL_ROBUSTNESS_TESTS_COMPLETE",
  "strategy_id": "string",
  "timestamp": "ISO-8601",
  "provider": "openai",
  "model": "gpt-4o",
  "prompt_hash": "md5",
  "input_artifacts": ["path1", "path2"],
  "output_artifact": "path"
}
```

## Validation

The `validate.py` script checks:

- Required files exist
- JSON files are valid
- Every spec has ≥3 explicit ambiguities
- Strategy C flagged as high risk
- Audit logs contain both formalisation and critique stages

Run with `make validate` or `python validate.py`.

## Key Design Principles

1. **No LLM Math**: All backtesting and metric calculations use deterministic Python/pandas
2. **Preserve Ambiguity**: Strategy formalisation surfaces assumptions rather than silently resolving them
3. **Martingale Warnings**: Loss-escalation strategies are explicitly flagged as high risk
4. **Reproducibility**: Synthetic data uses fixed seeds; all parameters documented
5. **Audit Trail**: Every LLM call logged with prompt hashes and input/output artifacts

## Project Structure

```
windsurf-project/
├── main.py                 # Pipeline orchestration
├── validate.py             # Output validation script
├── strategies.json         # Input strategy descriptions
├── Makefile                # Convenience commands
├── .gitignore             # Excludes .env and artifacts
├── specs/                 # Formal strategy specs (generated)
├── ledgers/               # Per-trade backtest results (generated)
├── data_manifest.json     # Data source documentation (generated)
├── metrics.json           # Performance metrics (generated)
├── critiques.json         # LLM risk assessments (generated)
├── walk_forward.json      # Stability analysis (generated)
├── parameter_sensitivity.json  # Sensitivity results (generated)
├── report.md              # Human-readable summary (generated)
└── llm_calls.jsonl        # LLM audit log (generated)
```

## Environment Variables

- `OPENAI_API_KEY`: Required for LLM calls

## Commands

| Command | Description |
|---------|-------------|
| `make run` | Execute full pipeline |
| `make validate` | Validate generated artifacts |
| `make clean` | Remove all generated files |
| `uv run main.py` | Run pipeline directly |
| `uv run validate.py` | Run validation directly |

## Notes for Evaluators

- The pipeline is designed to run from a clean checkout
- Generated artifacts may be deleted before evaluation
- `strategies.json` may be replaced with equivalent descriptions
- All numeric computations are deterministic Python code
- Strategy C must be flagged as high risk when the public fixture is used
- Static precomputed outputs are not sufficient; the pipeline must regenerate artifacts

## License

This is technical assessment tooling. Not financial advice.
