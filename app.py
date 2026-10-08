import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

BASE_URL = "https://fapi.bitunix.com"
TIMEOUT = 15
KLINE_LIMIT = 200
MAX_WORKERS = 8

# -----------------------------
# Shared Bitunix API
# -----------------------------
session = requests.Session()
session.headers.update({"Content-Type": "application/json"})


def api_json(path, params=None):
    r = session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    if str(payload.get("code", 0)) not in ("0", "None"):
        raise RuntimeError(payload)
    return payload.get("data", [])


@st.cache_data(ttl=300)
def get_trading_pairs():
    rows = api_json("/api/v1/futures/market/trading_pairs")
    symbols = []
    for x in rows:
        symbol = str(x.get("symbol", "")).upper()
        quote = str(x.get("quote", "")).upper()
        status = str(x.get("symbolStatus", "")).upper()
        if quote == "USDT" and status == "OPEN" and symbol and symbol != "BTCUSDT":
            symbols.append(symbol)
    return sorted(set(symbols))


def get_klines(symbol, interval, limit=KLINE_LIMIT):
    rows = api_json(
        "/api/v1/futures/market/kline",
        {"symbol": symbol, "interval": interval, "limit": limit, "type": "LAST_PRICE"},
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in ["open", "high", "low", "close", "quoteVol", "baseVol", "time"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    required = ["open", "high", "low", "close", "time"]
    if not all(c in df.columns for c in required):
        return pd.DataFrame()
    return df.dropna(subset=required).sort_values("time").drop_duplicates("time").reset_index(drop=True)


def clean_symbol(symbol):
    return symbol.replace("USDT", "")


# ============================================================
# MOVING AVERAGE SCREENER — preserved previous scanner logic
# ============================================================
EMA_LEN = 19
SMA_LEN = 23
MAJOR_LEN = 100
CON_BARS = 5
MAX_RANGE_PCT = 8.0
NO_TOUCH_BARS = 50


def add_ma_indicators(df):
    df = df.copy()
    df["ema19"] = df["close"].ewm(span=EMA_LEN, adjust=False, min_periods=EMA_LEN).mean()
    df["sma23"] = df["close"].rolling(SMA_LEN, min_periods=SMA_LEN).mean()
    df["sma100"] = df["close"].rolling(MAJOR_LEN, min_periods=MAJOR_LEN).mean()
    return df


def scan_ma_symbol(symbol, interval):
    try:
        df = get_klines(symbol, interval)
        if len(df) < 160:
            return None
        df = add_ma_indicators(df)
        signal_i = len(df) - 2  # latest CLOSED candle
        if signal_i < 1:
            return None

        close = float(df.loc[signal_i, "close"])
        high = float(df.loc[signal_i, "high"])
        low = float(df.loc[signal_i, "low"])
        ema19 = float(df.loc[signal_i, "ema19"])
        sma23 = float(df.loc[signal_i, "sma23"])
        sma100 = float(df.loc[signal_i, "sma100"])
        if any(pd.isna(x) for x in [ema19, sma23, sma100]):
            return None

        con_end = signal_i - 1
        con_start = signal_i - CON_BARS
        if con_start < 0:
            return None
        con = df.iloc[con_start:con_end + 1]
        if len(con) != CON_BARS:
            return None
        con_high = float(con["high"].max())
        con_low = float(con["low"].min())
        if con_low == 0:
            return None
        range_pct = (con_high - con_low) / con_low * 100.0

        long_conditions = [
            range_pct <= MAX_RANGE_PCT,
            bool((con["close"] - con["ema19"]).min() > 0),
            bool((con["ema19"] - con["sma23"]).min() > 0),
            bool((con["sma100"] - con["close"]).min() > 0),
            bool((con["sma100"] - con["ema19"]).min() > 0),
            bool((con["sma100"] - con["sma23"]).min() > 0),
        ]
        nt_end = signal_i - CON_BARS - 1
        nt_start = nt_end - NO_TOUCH_BARS + 1
        if nt_start < 0:
            return None
        no_touch = df.iloc[nt_start:nt_end + 1]
        if len(no_touch) != NO_TOUCH_BARS:
            return None
        long_no_touch = bool((no_touch["high"] - no_touch["sma100"]).max() < 0)
        long_ready = all(long_conditions) and long_no_touch

        short_conditions = [
            range_pct <= MAX_RANGE_PCT,
            bool((con["close"] - con["ema19"]).max() < 0),
            bool((con["ema19"] - con["sma23"]).max() < 0),
            bool((con["sma100"] - con["close"]).max() < 0),
            bool((con["sma100"] - con["ema19"]).max() < 0),
            bool((con["sma100"] - con["sma23"]).max() < 0),
        ]
        short_no_touch = bool((no_touch["low"] - no_touch["sma100"]).min() > 0)
        short_ready = all(short_conditions) and short_no_touch

        return {
            "symbol": symbol,
            "price": close,
            "distance100": abs(close - sma100) / sma100 * 100.0 if sma100 else None,
            "long_ready": long_ready,
            "long_breakout": bool(long_ready and close > con_high and close > sma100),
            "short_ready": short_ready,
            "short_breakdown": bool(short_ready and close < con_low and close < sma100),
            "range_pct": range_pct,
            "candle_time": int(df.loc[signal_i, "time"]),
        }
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}


def scan_ma_all(symbols, interval, workers=MAX_WORKERS):
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(scan_ma_symbol, s, interval) for s in symbols]
        for f in as_completed(futures):
            r = f.result()
            if r is not None:
                out.append(r)
    return out


def ma_tables(results):
    long_rows, short_rows = [], []
    for x in results:
        if x.get("error"):
            continue
        if x["long_ready"]:
            long_rows.append({
                "#": 0, "Coin": clean_symbol(x["symbol"]), "Price": x["price"],
                "Breakout": "BREAKOUT" if x["long_breakout"] else "CONSOLIDATED",
                "_distance": x["distance100"] if x["distance100"] is not None else 999999,
            })
        if x["short_ready"]:
            short_rows.append({
                "#": 0, "Coin": clean_symbol(x["symbol"]), "Price": x["price"],
                "Breakdown": "BREAKDOWN" if x["short_breakdown"] else "CONSOLIDATED",
                "_distance": x["distance100"] if x["distance100"] is not None else 999999,
            })
    long_rows.sort(key=lambda x: x["_distance"])
    short_rows.sort(key=lambda x: x["_distance"])
    for i, r in enumerate(long_rows, 1): r["#"] = i
    for i, r in enumerate(short_rows, 1): r["#"] = i
    ldf = pd.DataFrame(long_rows, columns=["#", "Coin", "Price", "Breakout"])
    sdf = pd.DataFrame(short_rows, columns=["#", "Coin", "Price", "Breakdown"])
    if ldf.empty:
        ldf = pd.DataFrame([{ "#": "-", "Coin": "NO LONG SETUPS", "Price": "-", "Breakout": "-" }])
    if sdf.empty:
        sdf = pd.DataFrame([{ "#": "-", "Coin": "NO SHORT SETUPS", "Price": "-", "Breakdown": "-" }])
    return ldf, sdf


def style_ma(df, side):
    target = "Breakout" if side == "LONG" else "Breakdown"
    def f(v):
        if side == "LONG" and v == "BREAKOUT": return "color: #00ff66; font-weight: bold"
        if side == "SHORT" and v == "BREAKDOWN": return "color: #ff4444; font-weight: bold"
        if v == "CONSOLIDATED": return "font-weight: bold"
        return ""
    return df.style.map(f, subset=[target])


# ============================================================
# MACD SCREENER
# ============================================================
MACD_CFG = {
    "15m": {"btc": "15m", "tf1": "3m", "tf2": "1m", "fast": 60},
    "1H":  {"btc": "1h",  "tf1": "15m", "tf2": "3m", "fast": 180},
    "4H":  {"btc": "4h",  "tf1": "1h",  "tf2": "15m", "fast": 900},
    "1D":  {"btc": "1d",  "tf1": "4h",  "tf2": "1h", "fast": 3600},
    "1W":  {"btc": "1w",  "tf1": "1d", "tf2": "4h", "fast": 14400},
}


def macd_series(close):
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    return macd, signal


def interval_seconds(interval):
    m = {"1m":60, "3m":180, "15m":900, "1h":3600, "4h":14400, "1d":86400, "1w":604800}
    return m[interval]


def closed_target(ts, seconds):
    # Latest fully closed boundary. Subtract 1 second to avoid selecting a candle
    # that has just started at the exact boundary.
    return ((int(ts) - 1) // seconds) * seconds * 1000


def macd_value_at(df, target_ms):
    if df.empty:
        return None
    sec = interval_seconds_from_df(df)
    x = df.copy()
    # Bitunix kline time is candle-open time; candle close = open + interval.
    x["close_ms"] = x["time"] + sec * 1000
    x = x[x["close_ms"] <= target_ms]
    if len(x) < 40:
        return None
    macd, signal = macd_series(x["close"])
    v = macd.iloc[-1]
    s = signal.iloc[-1]
    if pd.isna(v) or pd.isna(s):
        return None
    return float(v), float(s), int(x.iloc[-1]["close_ms"])


def interval_seconds_from_df(df):
    # Set by the caller through an attribute when possible; fallback is inferred.
    sec = getattr(df, "_interval_seconds", None)
    if sec:
        return sec
    if len(df) >= 2:
        d = int(df["time"].diff().dropna().median() / 1000)
        if d > 0:
            return d
    return 60


def fetch_tagged(symbol, interval):
    df = get_klines(symbol, interval)
    if not df.empty:
        # pandas does not reliably retain arbitrary attrs through all operations,
        # so use an explicit attribute immediately before calculation.
        df._interval_seconds = interval_seconds(interval)
    return df


def macd_state(df, target_ms):
    if df.empty:
        return None
    sec = interval_seconds_from_df(df)
    x = df.copy()
    x["close_ms"] = x["time"] + sec * 1000
    x = x[x["close_ms"] <= target_ms]
    if len(x) < 40:
        return None
    m, s = macd_series(x["close"])
    mv, sv = m.iloc[-1], s.iloc[-1]
    if pd.isna(mv) or pd.isna(sv):
        return None
    return float(mv), float(sv), int(x.iloc[-1]["close_ms"])


def classify_macd(v):
    if v is None:
        return "NEUTRAL"
    m, s, _ = v
    if m > 0 and m > s:
        return "BULLISH"
    if m < 0 and m < s:
        return "BEARISH"
    return "NEUTRAL"


def macd_scan(selected_tf, symbols, workers=MAX_WORKERS):
    cfg = MACD_CFG[selected_tf]
    now_ms = int(time.time() * 1000)
    target_ms = closed_target(now_ms / 1000, cfg["fast"])

    # BTC is the market-direction filter. It is always refreshed.
    btc_df = fetch_tagged("BTCUSDT", cfg["btc"])
    btc_state = macd_state(btc_df, target_ms)
    direction = classify_macd(btc_state)
    if direction == "NEUTRAL":
        return [], direction, target_ms

    setup = "LONG" if direction == "BULLISH" else "SHORT"

    def one(symbol):
        try:
            a = fetch_tagged(symbol, cfg["tf1"])
            b = fetch_tagged(symbol, cfg["tf2"])
            s1 = macd_state(a, target_ms)
            s2 = macd_state(b, target_ms)
            c1, c2 = classify_macd(s1), classify_macd(s2)
            if setup == "LONG" and c1 == "BULLISH" and c2 == "BEARISH":
                return (symbol, "🟢 LONG")
            if setup == "SHORT" and c1 == "BEARISH" and c2 == "BULLISH":
                return (symbol, "🔴 SHORT")
        except Exception:
            return None
        return None

    results = []
    # Batch/concurrency is shared across all coins; individual failures are skipped.
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(one, s) for s in symbols]
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)
    results.sort(key=lambda x: x[0])
    return results, direction, target_ms


# ============================================================
# UI
# ============================================================
st.set_page_config(page_title="Bitunix Screener", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

# Responsive, dark trading-dashboard styling. Works in desktop and mobile browsers.
st.markdown("""
<style>
:root { color-scheme: dark; }
[data-testid="stAppViewContainer"] { background: #0b1220; }
[data-testid="stHeader"] { background: rgba(11,18,32,.94); }
.block-container { padding-top: 1.1rem; padding-bottom: 2rem; max-width: 1400px; }
.hero { padding: 1.1rem 1.25rem; border: 1px solid #26344b; border-radius: 18px; background: linear-gradient(135deg,#121f33 0%,#101827 65%,#102b2a 100%); margin-bottom: 1rem; }
.hero-kicker { color: #67e8c1; font-size: .72rem; letter-spacing: .16em; font-weight: 800; text-transform: uppercase; }
.hero-title { color: #f4f7fb; font-size: clamp(1.55rem, 4vw, 2.35rem); line-height: 1.15; font-weight: 800; margin: .35rem 0; }
.hero-sub { color: #aab8ca; font-size: .92rem; }
div[data-testid="stTabs"] button { border-radius: 12px 12px 0 0; font-weight: 700; }
.stButton button { border-radius: 12px; min-height: 2.8rem; font-weight: 800; border: 1px solid #2dd4bf; }
div[data-testid="stSelectbox"] > label, div[data-testid="stNumberInput"] > label { color: #c8d4e3; font-weight: 650; }
[data-testid="stDataFrame"] { border: 1px solid #26344b; border-radius: 12px; overflow: hidden; }
[data-testid="stAlert"] { border-radius: 12px; }
hr { border-color: #26344b; }
.small-note { color: #91a2b8; font-size: .78rem; }
@media (max-width: 700px) {
 .block-container { padding: .65rem .7rem 1.2rem .7rem; }
 .hero { padding: .9rem; border-radius: 14px; }
 .hero-sub { font-size: .82rem; }
 div[data-testid="stTabs"] [role="tab"] { padding: .5rem .6rem; font-size: .82rem; }
 div[data-testid="stHorizontalBlock"] { gap: .5rem; }
 .stButton button { width: 100%; min-height: 3rem; }
 [data-testid="stDataFrame"] { font-size: .82rem; }
}
</style>
<div class="hero">
  <div class="hero-kicker">LIVE MARKET SCANNER</div>
  <div class="hero-title">Bitunix Screener</div>
  <div class="hero-sub">Moving Average setups · MACD multi-timeframe signals · Closed candles only</div>
</div>
""", unsafe_allow_html=True)

try:
    symbols = get_trading_pairs()
except Exception as exc:
    st.error(f"NO CONNECTION — unable to load Bitunix trading pairs: {exc}")
    st.stop()

ma_tab, macd_tab = st.tabs(["📊 Moving Average Screener", "📈 MACD Screener"])

with ma_tab:
    st.subheader("Moving Average Screener")
    st.caption("EMA 19 / SMA 23 / SMA 100 · 5-candle consolidation · 50-candle no-touch")
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        ma_interval = st.selectbox("Timeframe", ["15m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"], index=1, key="ma_tf")
    with c2:
        ma_workers = st.number_input("Workers", 1, 10, 8, key="ma_workers")
    with c3:
        ma_scan = st.button("SCAN NOW", key="ma_scan", use_container_width=True)

    # Scan automatically when the tab first loads; Streamlit reruns the app on changes.
    if ma_scan or "ma_results" not in st.session_state or st.session_state.get("ma_last_tf") != ma_interval:
        with st.spinner("Scanning Moving Average setups..."):
            try:
                st.session_state.ma_results = scan_ma_all(symbols, ma_interval, int(ma_workers))
                st.session_state.ma_last_tf = ma_interval
            except Exception as exc:
                st.session_state.ma_results = []
                st.error(f"NO CONNECTION: {exc}")

    results = st.session_state.get("ma_results", [])
    ldf, sdf = ma_tables(results)
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("LONG")
        st.dataframe(style_ma(ldf, "LONG"), use_container_width=True, hide_index=True)
    with col2:
        st.subheader("SHORT")
        st.dataframe(style_ma(sdf, "SHORT"), use_container_width=True, hide_index=True)

    lb = [clean_symbol(x["symbol"]) for x in results if not x.get("error") and x.get("long_breakout")]
    sb = [clean_symbol(x["symbol"]) for x in results if not x.get("error") and x.get("short_breakdown")]
    if lb:
        st.success("LONG BREAKOUT: " + ", ".join(lb))
    if sb:
        st.error("SHORT BREAKDOWN: " + ", ".join(sb))

with macd_tab:
    st.subheader("MACD Screener")
    st.caption("MACD 12 / 26 / 9 · Closed candles only · BTC directional filter")
    c1, c2 = st.columns([2, 1])
    with c1:
        macd_tf = st.selectbox("Timeframe", list(MACD_CFG.keys()), index=2, key="macd_tf")
    with c2:
        macd_scan_btn = st.button("SCAN NOW", key="macd_scan", use_container_width=True)

    if macd_scan_btn or "macd_results" not in st.session_state or st.session_state.get("macd_last_tf") != macd_tf:
        with st.spinner("Scanning MACD setups..."):
            try:
                r, direction, target = macd_scan(macd_tf, symbols, MAX_WORKERS)
                st.session_state.macd_results = r
                st.session_state.macd_direction = direction
                st.session_state.macd_target = target
                st.session_state.macd_last_tf = macd_tf
            except Exception as exc:
                st.session_state.macd_results = []
                st.session_state.macd_direction = "NO CONNECTION"
                st.error(f"NO CONNECTION: {exc}")

    r = st.session_state.get("macd_results", [])
    direction = st.session_state.get("macd_direction", "NEUTRAL")
    if direction == "BULLISH":
        st.success("BTC DIRECTION: BULLISH — LONG setups only")
    elif direction == "BEARISH":
        st.error("BTC DIRECTION: BEARISH — SHORT setups only")
    elif direction == "NO CONNECTION":
        st.error("NO CONNECTION")

    if r:
        st.dataframe(pd.DataFrame(r, columns=["COIN", "SETUP"]), use_container_width=True, hide_index=True)
    elif direction != "NO CONNECTION":
        st.info("NO SETUPS FOUND")

st.divider()
st.markdown(f'<div class="small-note">Bitunix open USDT perpetuals: <b>{len(symbols)}</b> · Market data from Bitunix public API · Not financial advice</div>', unsafe_allow_html=True)
