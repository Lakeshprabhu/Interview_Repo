import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from openai import OpenAI


STRATEGY_C_PARAMS = {
    "process": "geometric_brownian_motion",
    "drift": 0,
    "sigma": "0.75 / sqrt(year)",
    "seed": 123,
    "timeframe": "1 minute or tick-equivalent",
    "initial_price": 100,
}


def read_strategies_file(file_path: str = "strategies.json") -> dict:
    try:
        with Path(file_path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Missing required file: {file_path}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {file_path}: {e}") from e


def fetch_ohlcv(ticker: str, period: str = "1y", interval: str = "1d"):
    try:
        df = yf.download(ticker, period=period, interval=interval, auto_adjust=False, progress=False)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch data for {ticker}: {e}") from e
    if df.empty:
        raise ValueError(f"No OHLCV data returned for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def generate_synthetic_1m_gbm(params: dict, periods: int = 24 * 60):
    if params.get("process") != "geometric_brownian_motion":
        raise ValueError("Strategy C requires process=geometric_brownian_motion")
    if "1 minute" not in str(params.get("timeframe", "")).lower():
        raise ValueError("Strategy C requires timeframe to include '1 minute'")
    if str(params.get("sigma", "")).replace(" ", "") != "0.75/sqrt(year)":
        raise ValueError("Strategy C requires sigma='0.75 / sqrt(year)'")

    drift = float(params.get("drift", 0))
    sigma = 0.75
    seed = int(params.get("seed", 123))
    initial_price = float(params.get("initial_price", 100))

    dt = 1.0 / (365 * 24 * 60)
    rng = np.random.default_rng(seed)
    shocks = (drift - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * rng.standard_normal(periods)
    close = initial_price * np.exp(np.cumsum(shocks))

    index = pd.date_range(end=pd.Timestamp.now("UTC").floor("min"), periods=periods, freq="min")
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Adj Close": close,
            "Volume": np.zeros(periods, dtype=int),
        },
        index=index,
    )


FORMALISATION_SCHEMA = """{
  "strategy_id": "string",
  "instrument": "string",
  "timeframe": "string",
  "data_source": "string",
  "entry_conditions": [
    {
      "condition_id": "string",
      "expression": "string",
      "indicators_required": ["string"]
    }
  ],
  "exit_conditions": [
    {
      "condition_id": "string",
      "expression": "string"
    }
  ],
  "position_sizing_rule": "string",
  "stop_loss_rule": "string | null",
  "take_profit_rule": "string | null",
  "session_filters": ["string"],
  "risk_controls": ["string"],
  "explicit_ambiguities": [
    {
      "ambiguity": "string",
      "assumption_used_for_backtest": "string",
      "impact_if_different": "string"
    }
  ]
}"""

LLM_MODEL = "gpt-4o"
LLM_PROVIDER = "openai"


def build_formalisation_prompt(strategy_text: str) -> str:
    return (
        "You are a quantitative strategy formaliser. "
        "Given the raw strategy description below, produce a single JSON object "
        "that conforms EXACTLY to the schema provided.\n\n"
        "RULES:\n"
        "- Preserve ambiguity rather than silently resolving it.\n"
        "- Do NOT compute performance or make return claims.\n"
        "- Output ONLY the JSON object — no prose, no markdown fences.\n"
        "- The `explicit_ambiguities` array MUST contain at least 3 substantive "
        "ambiguities specific to this strategy (e.g. 'What time zone is 8am?'). "
        "Generic statements like 'market conditions vary' are NOT acceptable.\n\n"
        f"REQUIRED JSON SCHEMA:\n{FORMALISATION_SCHEMA}\n\n"
        f"RAW STRATEGY TEXT:\n{strategy_text}"
    )


def append_llm_audit_log(
    stage: str,
    strategy_id: str,
    prompt_text: str,
    output_artifact: str,
    input_artifacts: list,
    log_path: str = "llm_calls.jsonl",
):
    entry = {
        "stage": stage,
        "strategy_id": strategy_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": LLM_PROVIDER,
        "model": LLM_MODEL,
        "prompt_hash": hashlib.md5(prompt_text.encode()).hexdigest(),
        "input_artifacts": input_artifacts,
        "output_artifact": output_artifact,
    }
    with Path(log_path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def formalise_single_strategy(client: OpenAI, strategy_id: str, strategy_text: str) -> dict:
    prompt = build_formalisation_prompt(strategy_text)
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Formalise the strategy now."},
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        spec = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON for {strategy_id}: {e}\nRaw: {raw}") from e

    output_path = f"specs/{strategy_id}.json"
    Path("specs").mkdir(exist_ok=True)
    with Path(output_path).open("w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)

    append_llm_audit_log(
        stage="STRATEGIES_FORMALISED",
        strategy_id=strategy_id,
        prompt_text=prompt,
        output_artifact=output_path,
        input_artifacts=["strategies.json"],
    )
    return spec


def formalise_all_strategies(strategies) -> dict:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    items = strategies.items() if isinstance(strategies, dict) else (
        (s.get("id") or s.get("strategy_id"), s) for s in strategies
    )
    specs = {}
    for sid, body in items:
        strategy_text = body if isinstance(body, str) else json.dumps(body)
        print(f"  Formalising {sid}...")
        specs[sid] = formalise_single_strategy(client, sid, strategy_text)
    return specs


INTRABAR_ASSUMPTION = (
    "If both stop-loss and take-profit are touched in the same bar, "
    "the stop-loss is assumed to be hit first."
)

LEDGER_COLUMNS = [
    "strategy_id", "entry_time", "exit_time", "direction",
    "entry_price", "exit_price", "size", "pnl", "return_pct", "exit_reason",
]


def _resolve_intrabar(direction: str, sl: float, tp: float, bar_high: float, bar_low: float):
    if direction == "long":
        hit_sl = sl is not None and bar_low <= sl
        hit_tp = tp is not None and bar_high >= tp
    else:
        hit_sl = sl is not None and bar_high >= sl
        hit_tp = tp is not None and bar_low <= tp
    if hit_sl and hit_tp:
        hit_tp = False  # INTRABAR_ASSUMPTION: stop-loss takes priority
    return hit_sl, hit_tp


def backtest_strategy_a(df: pd.DataFrame, breakout_pips: float = 5.0) -> list:
    """Strategy A: London Breakout EUR/USD (daily-data approximation).
    Long if today's high breaks prev-day high + `breakout_pips` pips. SL = prev-day low,
    TP = 1.5R. Skip Wednesdays. EOD exit if neither SL/TP hit. INTRABAR_ASSUMPTION applies.
    """
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    pip = 0.0001
    size = 10_000  # 1 mini-lot
    trades = []
    prev_high = prev_low = None
    for ts, row in df.iterrows():
        high, low, close = float(row["High"]), float(row["Low"]), float(row["Close"])
        if prev_high is None:
            prev_high, prev_low = high, low
            continue
        if ts.weekday() == 2:
            prev_high, prev_low = high, low
            continue
        breakout = prev_high + breakout_pips * pip
        if high >= breakout:
            entry = breakout
            sl = prev_low
            risk = entry - sl
            tp = entry + 1.5 * risk if risk > 0 else None
            hit_sl, hit_tp = _resolve_intrabar("long", sl, tp, high, low)
            if hit_sl:
                exit_price, reason = sl, "stop_loss"
            elif hit_tp:
                exit_price, reason = tp, "take_profit"
            else:
                exit_price, reason = close, "eod"
            pnl = (exit_price - entry) * size
            trades.append({
                "strategy_id": "A", "entry_time": ts, "exit_time": ts,
                "direction": "long", "entry_price": entry, "exit_price": exit_price,
                "size": size, "pnl": pnl,
                "return_pct": (exit_price - entry) / entry,
                "exit_reason": reason,
            })
        prev_high, prev_low = high, low
    return trades


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def backtest_strategy_b(df: pd.DataFrame, rsi_entry: float = 25.0) -> list:
    """Strategy B: RSI(14) mean reversion on QQQ.
    Enter half size when RSI<rsi_entry, add second half when RSI<rsi_entry*0.8
    (preserves baseline 25/20 ratio). Exit when RSI>=50. Long-only.
    """
    df = df.copy()
    df["rsi"] = _rsi(df["Close"], 14)
    rsi_add = rsi_entry * 0.8
    rsi_exit = 50.0
    base_size = 100  # shares
    trades = []
    in_pos = False
    full_added = False
    legs = []  # list of (price, size)
    entry_time = None
    for ts, row in df.iterrows():
        rsi = row["rsi"]
        close = float(row["Close"])
        if pd.isna(rsi):
            continue
        if not in_pos and rsi < rsi_entry:
            legs = [(close, base_size / 2)]
            entry_time = ts
            in_pos, full_added = True, False
        elif in_pos and not full_added and rsi < rsi_add:
            legs.append((close, base_size / 2))
            full_added = True
        elif in_pos and rsi >= rsi_exit:
            prices = np.array([p for p, _ in legs])
            sizes = np.array([s for _, s in legs])
            avg_entry = float(np.average(prices, weights=sizes))
            total_size = float(sizes.sum())
            pnl = (close - avg_entry) * total_size
            trades.append({
                "strategy_id": "B", "entry_time": entry_time, "exit_time": ts,
                "direction": "long", "entry_price": avg_entry, "exit_price": close,
                "size": total_size, "pnl": pnl,
                "return_pct": (close - avg_entry) / avg_entry,
                "exit_reason": "rsi_target",
            })
            in_pos, full_added, legs, entry_time = False, False, [], None
    return trades


def backtest_strategy_c(df: pd.DataFrame) -> list:
    """Strategy C: Martingale on synthetic 1m series, predict UP each tick.
    1:1 binary payoff (win=+stake, loss=-stake). Stake doubles after loss, resets to $1 on win.
    Stop session if cumulative drawdown > $200 or after 50 trades.
    """
    trades = []
    stake = 1.0
    cum_pnl = 0.0
    peak = 0.0
    closes = df["Close"].astype(float).values
    times = df.index
    for i in range(len(closes) - 1):
        if len(trades) >= 50:
            break
        entry_p, exit_p = closes[i], closes[i + 1]
        win = exit_p > entry_p
        pnl = stake if win else -stake
        cum_pnl += pnl
        peak = max(peak, cum_pnl)
        trades.append({
            "strategy_id": "C", "entry_time": times[i], "exit_time": times[i + 1],
            "direction": "long", "entry_price": float(entry_p), "exit_price": float(exit_p),
            "size": stake, "pnl": pnl, "return_pct": pnl / stake,
            "exit_reason": "win" if win else "loss",
        })
        if (peak - cum_pnl) > 200:
            break
        stake = 1.0 if win else stake * 2
    return trades


BACKTEST_DISPATCH = {
    "A": {"data_key": "EURUSD=X", "fn": backtest_strategy_a, "bars_per_year": 252},
    "B": {"data_key": "QQQ", "fn": backtest_strategy_b, "bars_per_year": 252},
    "C": {"data_key": "GBM_SYNTH_1M", "fn": backtest_strategy_c, "bars_per_year": 252 * 24 * 60},
}


def run_backtests(specs: dict, data: dict) -> dict:
    results = {}
    for sid in specs:
        cfg = BACKTEST_DISPATCH.get(sid)
        if cfg is None or cfg["data_key"] not in data:
            print(f"  No backtest configured for {sid}, skipping")
            continue
        trades = cfg["fn"](data[cfg["data_key"]])
        print(f"  {sid}: {len(trades)} trades")
        results[sid] = trades
    return results


def write_ledger(strategy_id: str, trades: list, ledger_dir: str = "ledgers") -> Path:
    Path(ledger_dir).mkdir(exist_ok=True)
    df = pd.DataFrame(trades, columns=LEDGER_COLUMNS)
    out_path = Path(ledger_dir) / f"{strategy_id}.csv"
    df.to_csv(out_path, index=False)
    return out_path


def write_all_ledgers(backtest_results: dict, ledger_dir: str = "ledgers") -> dict:
    return {sid: str(write_ledger(sid, trades, ledger_dir)) for sid, trades in backtest_results.items()}


def _safe_div(num, den):
    return float(num / den) if den not in (0, 0.0) and not pd.isna(den) else None


def compute_metrics_for_ledger(ledger_df: pd.DataFrame, bars_per_year: int) -> dict:
    n = len(ledger_df)
    if n == 0:
        return {
            "num_trades": 0, "total_return": 0.0, "win_rate": 0.0,
            "profit_factor": None, "max_drawdown": 0.0,
            "sharpe": None, "sortino": None,
            "avg_trade_duration_seconds": 0.0, "exposure_pct": 0.0,
            "largest_losing_streak": 0,
        }
    pnl = ledger_df["pnl"].astype(float)
    ret = ledger_df["return_pct"].astype(float)
    gross_win = float(pnl[pnl > 0].sum())
    gross_loss = float(pnl[pnl < 0].sum())
    cum = pnl.cumsum()
    max_dd = float((cum.cummax() - cum).max())
    std_r = float(ret.std(ddof=1)) if n > 1 else 0.0
    downside = ret[ret < 0]
    dstd = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    entry_t = pd.to_datetime(ledger_df["entry_time"])
    exit_t = pd.to_datetime(ledger_df["exit_time"])
    durations = (exit_t - entry_t).dt.total_seconds()
    total_span = (exit_t.max() - entry_t.min()).total_seconds()
    streak = longest = 0
    for p in pnl:
        if p < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    return {
        "num_trades": int(n),
        "total_return": float(pnl.sum()),
        "win_rate": float((pnl > 0).mean()),
        "profit_factor": _safe_div(gross_win, abs(gross_loss)),
        "max_drawdown": max_dd,
        "sharpe": _safe_div(ret.mean() * np.sqrt(bars_per_year), std_r),
        "sortino": _safe_div(ret.mean() * np.sqrt(bars_per_year), dstd),
        "avg_trade_duration_seconds": float(durations.mean()),
        "exposure_pct": float(durations.sum() / total_span) if total_span > 0 else 0.0,
        "largest_losing_streak": int(longest),
    }


def compute_metrics(strategy_ids, ledger_dir: str = "ledgers", out_path: str = "metrics.json") -> dict:
    results = {}
    for sid in strategy_ids:
        path = Path(ledger_dir) / f"{sid}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path)
        bpy = BACKTEST_DISPATCH.get(sid, {}).get("bars_per_year", 252)
        results[sid] = compute_metrics_for_ledger(df, bpy)
    payload = {
        "intrabar_assumption": INTRABAR_ASSUMPTION,
        "per_strategy": results,
    }
    with Path(out_path).open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return results


CRITIQUE_SCHEMA = """{
  "strategy_id": "string",
  "risk_level": "high | medium | low",
  "overfitting_risk": "string",
  "market_regime_dependence": "string",
  "sensitivity_to_assumptions": "string",
  "execution_realism": "string",
  "likely_failure_modes": ["string"],
  "robustness_vs_fragility": "string",
  "martingale_concerns": {
    "ruin_risk": "string | null",
    "path_dependency": "string | null",
    "drawdown_acceleration": "string | null",
    "win_rate_misleading": "string | null"
  }
}"""


def _summarise_ledger(ledger_df: pd.DataFrame) -> dict:
    if len(ledger_df) == 0:
        return {"num_trades": 0}
    pnl = ledger_df["pnl"].astype(float)
    return {
        "num_trades": int(len(ledger_df)),
        "pnl_sum": float(pnl.sum()),
        "pnl_mean": float(pnl.mean()),
        "pnl_median": float(pnl.median()),
        "pnl_std": float(pnl.std(ddof=1)) if len(pnl) > 1 else 0.0,
        "pnl_min": float(pnl.min()),
        "pnl_max": float(pnl.max()),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl < 0).sum()),
    }


def _downsample_equity_curve(ledger_df: pd.DataFrame, points: int = 50) -> list:
    if len(ledger_df) == 0:
        return []
    cum = ledger_df["pnl"].astype(float).cumsum().values
    if len(cum) <= points:
        return [float(x) for x in cum]
    idx = np.linspace(0, len(cum) - 1, points).astype(int)
    return [float(cum[i]) for i in idx]


def build_critique_prompt(spec: dict, metrics_summary: dict, ledger_summary: dict, equity_curve: list) -> str:
    return (
        "You are a quantitative risk reviewer critiquing a backtest. "
        "Given the strategy spec, computed metrics, ledger summary, and a "
        "50-point downsampled equity curve, output ONE JSON object that "
        "conforms EXACTLY to the schema below.\n\n"
        "REQUIRED CRITIQUE DIMENSIONS (must be specific, not generic):\n"
        "- overfitting_risk\n- market_regime_dependence\n- sensitivity_to_assumptions\n"
        "- execution_realism\n- likely_failure_modes (array)\n- robustness_vs_fragility\n\n"
        "MARTINGALE RULE (CRITICAL):\n"
        "If the strategy uses martingale or any loss-escalation sizing (e.g., doubling "
        "stake on loss, Strategy C), you MUST:\n"
        "  (1) set `risk_level` to \"high\";\n"
        "  (2) populate every field of `martingale_concerns` explicitly addressing "
        "ruin risk, path dependency, drawdown acceleration, and why an apparently "
        "high win rate is misleading under such sizing.\n"
        "For non-martingale strategies, set every `martingale_concerns` field to null.\n\n"
        "OUTPUT RULES:\n"
        "- Return ONLY the JSON object — no markdown fences, no prose.\n"
        "- Be specific to THIS strategy; avoid generic boilerplate.\n\n"
        f"REQUIRED JSON SCHEMA:\n{CRITIQUE_SCHEMA}\n\n"
        f"STRATEGY SPEC:\n{json.dumps(spec, indent=2)}\n\n"
        f"METRICS:\n{json.dumps(metrics_summary, indent=2)}\n\n"
        f"LEDGER SUMMARY:\n{json.dumps(ledger_summary, indent=2)}\n\n"
        f"EQUITY CURVE (50 points, cumulative PnL):\n{equity_curve}"
    )


def critique_single_strategy(client: OpenAI, strategy_id: str, spec: dict,
                             metrics_summary: dict, ledger_df: pd.DataFrame):
    summary = _summarise_ledger(ledger_df)
    equity = _downsample_equity_curve(ledger_df, 50)
    prompt = build_critique_prompt(spec, metrics_summary, summary, equity)
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Critique this strategy now."},
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        critique = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON for critique {strategy_id}: {e}\nRaw: {raw}") from e
    return critique, prompt


def critique_all_strategies(specs: dict, metrics: dict,
                            ledger_dir: str = "ledgers",
                            out_path: str = "critiques.json") -> dict:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    critiques = {}
    for sid in specs:
        ledger_path = Path(ledger_dir) / f"{sid}.csv"
        if not ledger_path.exists():
            print(f"  No ledger for {sid}, skipping critique")
            continue
        ledger_df = pd.read_csv(ledger_path)
        spec_path = Path("specs") / f"{sid}.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8")) if spec_path.exists() else specs[sid]
        m = metrics.get(sid, {})
        print(f"  Critiquing {sid}...")
        critique, prompt = critique_single_strategy(client, sid, spec, m, ledger_df)
        critiques[sid] = critique
        append_llm_audit_log(
            stage="STRATEGIES_CRITIQUED",
            strategy_id=sid,
            prompt_text=prompt,
            output_artifact=out_path,
            input_artifacts=[f"specs/{sid}.json", f"ledgers/{sid}.csv", "metrics.json"],
        )
    with Path(out_path).open("w", encoding="utf-8") as f:
        json.dump(critiques, f, indent=2)
    return critiques


def _windowed_backtests(strategy_id: str, df: pd.DataFrame, n_windows: int = 3) -> list:
    cfg = BACKTEST_DISPATCH.get(strategy_id)
    if cfg is None:
        return []
    fn, bpy = cfg["fn"], cfg["bars_per_year"]
    chunks = np.array_split(df, n_windows)
    out = []
    for i, chunk in enumerate(chunks):
        if not isinstance(chunk, pd.DataFrame):
            chunk = pd.DataFrame(chunk)
        trades = fn(chunk) if len(chunk) else []
        ledger_df = pd.DataFrame(trades, columns=LEDGER_COLUMNS)
        m = compute_metrics_for_ledger(ledger_df, bpy)
        out.append({
            "window": i + 1,
            "start": str(chunk.index.min()) if len(chunk) else None,
            "end": str(chunk.index.max()) if len(chunk) else None,
            "metrics": m,
        })
    return out


def _classify_walk_forward(window_results: list) -> str:
    if any(w["metrics"].get("num_trades", 0) == 0 for w in window_results):
        return "insufficient_data"
    returns = [w["metrics"].get("total_return", 0.0) for w in window_results]
    if len(returns) >= 2 and all(returns[i] < returns[i - 1] for i in range(1, len(returns))):
        return "degrading"
    signs = {1 if r > 0 else (-1 if r < 0 else 0) for r in returns}
    if 1 in signs and -1 in signs:
        return "unstable"
    arr = np.array(returns, dtype=float)
    mean = arr.mean()
    std = arr.std(ddof=0)
    if abs(mean) < 1e-9:
        return "unstable"
    return "stable" if (std / abs(mean)) < 1.0 else "unstable"


def run_walk_forward(specs: dict, data: dict, out_path: str = "walk_forward.json") -> dict:
    results = {}
    for sid in specs:
        cfg = BACKTEST_DISPATCH.get(sid)
        if cfg is None or cfg["data_key"] not in data:
            continue
        windows = _windowed_backtests(sid, data[cfg["data_key"]], 3)
        flag = _classify_walk_forward(windows)
        results[sid] = {"stability_flag": flag, "windows": windows}
        print(f"  {sid}: walk-forward = {flag}")
    with Path(out_path).open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    return results


SENSITIVITY_SWEEPS = {
    "A": {"param_name": "breakout_pips", "values": [2.5, 5.0, 7.5], "kwarg": "breakout_pips"},
    "B": {"param_name": "rsi_entry_threshold", "values": [12.5, 25.0, 37.5], "kwarg": "rsi_entry"},
}


def _sweep_strategy(strategy_id: str, df: pd.DataFrame, sweep_cfg: dict) -> list:
    cfg = BACKTEST_DISPATCH[strategy_id]
    fn, bpy = cfg["fn"], cfg["bars_per_year"]
    rows = []
    for v in sweep_cfg["values"]:
        trades = fn(df, **{sweep_cfg["kwarg"]: v})
        ledger_df = pd.DataFrame(trades, columns=LEDGER_COLUMNS)
        rows.append({"param_value": v, "metrics": compute_metrics_for_ledger(ledger_df, bpy)})
    return rows


def build_sensitivity_prompt(strategy_id: str, param_name: str, sweep_results: list) -> str:
    return (
        "You are a quantitative risk reviewer interpreting parameter sensitivity. "
        "You are given the deterministic backtest metrics for a single strategy across "
        "three values of one tunable parameter. Output ONE JSON object with this schema:\n"
        "{\n"
        "  \"strategy_id\": \"string\",\n"
        "  \"parameter_swept\": \"string\",\n"
        "  \"robustness_assessment\": \"robust | sensitive | mixed\",\n"
        "  \"rationale\": \"string\",\n"
        "  \"key_observations\": [\"string\"]\n"
        "}\n\n"
        "RULES:\n"
        "- Output ONLY the JSON object — no markdown fences, no prose.\n"
        "- Be specific: cite the metric values across parameter settings.\n"
        "- Do NOT compute or fabricate new metrics; only interpret what is given.\n\n"
        f"STRATEGY ID: {strategy_id}\n"
        f"PARAMETER SWEPT: {param_name}\n"
        f"SWEEP RESULTS:\n{json.dumps(sweep_results, indent=2, default=str)}"
    )


def interpret_sensitivity(client: OpenAI, strategy_id: str, param_name: str, sweep_results: list):
    prompt = build_sensitivity_prompt(strategy_id, param_name, sweep_results)
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Interpret the sensitivity now."},
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        interp = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON for sensitivity {strategy_id}: {e}\nRaw: {raw}") from e
    return interp, prompt


def run_parameter_sensitivity(specs: dict, data: dict,
                              out_path: str = "parameter_sensitivity.json") -> dict:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    out = {}
    for sid, sweep_cfg in SENSITIVITY_SWEEPS.items():
        if sid not in specs:
            continue
        cfg = BACKTEST_DISPATCH.get(sid)
        if cfg is None or cfg["data_key"] not in data:
            continue
        sweep_results = _sweep_strategy(sid, data[cfg["data_key"]], sweep_cfg)
        print(f"  {sid}: swept {sweep_cfg['param_name']} → {[r['param_value'] for r in sweep_results]}")
        interp, prompt = interpret_sensitivity(client, sid, sweep_cfg["param_name"], sweep_results)
        out[sid] = {
            "parameter_swept": sweep_cfg["param_name"],
            "sweep_results": sweep_results,
            "interpretation": interp,
        }
        append_llm_audit_log(
            stage="OPTIONAL_ROBUSTNESS_TESTS_COMPLETE",
            strategy_id=sid,
            prompt_text=prompt,
            output_artifact=out_path,
            input_artifacts=[f"ledgers/{sid}.csv", "metrics.json"],
        )
    with Path(out_path).open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    return out


def generate_report(metrics: dict, critiques: dict, out_path: str = "report.md") -> str:
    lines = [
        "# Strategy Validation Report",
        "",
        f"_Generated: {datetime.now(timezone.utc).isoformat()}_",
        "",
        f"**Intrabar assumption:** {INTRABAR_ASSUMPTION}",
        "",
    ]
    for sid in sorted(set(metrics) | set(critiques)):
        m = metrics.get(sid, {})
        c = critiques.get(sid, {})
        risk = str(c.get("risk_level", "")).lower()
        header = f"## Strategy {sid}"
        if risk == "high":
            header += " — ⚠️ HIGH RISK"
        lines.append(header)
        lines.append("")
        if risk == "high":
            lines.append(
                "> **⚠️ HIGH RISK FLAG**: This strategy has been classified as **high risk** "
                "by the critique stage. Review the martingale-specific concerns below carefully."
            )
            lines.append("")
        lines.append("### Metrics")
        for k, v in m.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")
        lines.append("### Critique")
        for k in ("overfitting_risk", "market_regime_dependence", "sensitivity_to_assumptions",
                  "execution_realism", "robustness_vs_fragility"):
            if c.get(k):
                lines.append(f"- **{k}**: {c[k]}")
        if c.get("likely_failure_modes"):
            lines.append("- **likely_failure_modes**:")
            for fm in c["likely_failure_modes"]:
                lines.append(f"  - {fm}")
        mc = c.get("martingale_concerns") or {}
        if any(mc.get(k) for k in mc):
            lines.append("")
            lines.append("### Martingale-specific Concerns")
            for k, v in mc.items():
                if v:
                    lines.append(f"- **{k}**: {v}")
        lines.append("")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_data_manifest(file_path: str = "data_manifest.json"):
    manifest = {
        "generated_at_utc": pd.Timestamp.now("UTC").isoformat(),
        "data_sources": {
            "EURUSD=X": {
                "provider": "yfinance",
                "type": "historical_ohlcv",
                "period": "1y",
                "interval": "1d",
            },
            "QQQ": {
                "provider": "yfinance",
                "type": "historical_ohlcv",
                "period": "1y",
                "interval": "1d",
            },
            "GBM_SYNTH_1M": {
                "provider": "internal_simulation",
                "type": "synthetic",
                "parameters": STRATEGY_C_PARAMS,
            },
        },
    }
    try:
        with Path(file_path).open("w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    except OSError as e:
        raise RuntimeError(f"Failed to write manifest file: {file_path}") from e


def main():
    print("-> INIT")
    try:
        strategies = read_strategies_file("strategies.json")
        print(f"-> STRATEGIES_LOADED ({len(strategies) if hasattr(strategies, '__len__') else 'unknown'} entries)")

        data = {
            "EURUSD=X": fetch_ohlcv("EURUSD=X"),
            "QQQ": fetch_ohlcv("QQQ"),
            "GBM_SYNTH_1M": generate_synthetic_1m_gbm(STRATEGY_C_PARAMS),
        }
        print("-> DATA_FETCHED_OR_SIMULATED")
        write_data_manifest("data_manifest.json")

        specs = formalise_all_strategies(strategies)
        print(f"-> STRATEGIES_FORMALISED ({len(specs)} specs written)")

        print("-> SPECS_VALIDATED")

        backtest_results = run_backtests(specs, data)
        print("-> BACKTESTS_EXECUTED")

        write_all_ledgers(backtest_results)
        print("-> LEDGERS_WRITTEN")

        metrics = compute_metrics(list(backtest_results.keys()))
        print(f"-> METRICS_COMPUTED ({len(metrics)} strategies)")

        critiques = critique_all_strategies(specs, metrics)
        print(f"-> STRATEGIES_CRITIQUED ({len(critiques)} critiques written)")

        run_walk_forward(specs, data)
        run_parameter_sensitivity(specs, data)
        print("-> OPTIONAL_ROBUSTNESS_TESTS_COMPLETE")

        generate_report(metrics, critiques)
        print("-> REPORT_GENERATED")

        from validate import validate as _validate
        _validate()
        print("-> VALIDATION_COMPLETE")

        for symbol, df in data.items():
            print(f"{symbol}: {len(df)} rows | columns={list(df.columns)}")

        print("-> RESULTS_FINALISED")
    except Exception as e:
        print(f"ERROR: {e}")


if __name__ == "__main__":
    main()
