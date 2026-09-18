import os
import math
import time
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pyotp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from SmartApi import SmartConnect
import requests


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("ANGEL_API_KEY", "")
CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE", "")
PASSWORD = os.getenv("ANGEL_PASSWORD", "")
TOTP_KEY = os.getenv("ANGEL_TOTP_KEY", "")

app = FastAPI(title="F&O Scanner Live Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# F&O UNIVERSE
# ============================================================

FO_STOCKS = [
    "AARTIIND","ABB","ABBOTINDIA","ACC","ADANIENT","ADANIPORTS",
    "ABCAPITAL","ABFRL","ALKEM","AMBUJACEM","APOLLOHOSP",
    "APOLLOTYRE","ASHOKLEY","ASIANPAINT","ASTRAL","ATUL",
    "AUBANK","AUROPHARMA","AXISBANK","BAJAJ-AUTO","BAJFINANCE",
    "BAJAJFINSV","BALKRISIND","BALRAMCHIN","BANDHANBNK","BANKBARODA",
    "BATAINDIA","BEL","BHARATFORG","BHEL","BPCL","BHARTIARTL",
    "BIOCON","BSOFT","BOSCHLTD","BRITANNIA","CANFINHOME","CANBK",
    "CHAMBLFERT","CHOLAFIN","CIPLA","CUB","COALINDIA","COFORGE",
    "COLPAL","CONCOR","COROMANDEL","CROMPTON","CUMMINSIND",
    "DABUR","DALBHARAT","DEEPAKNTR","DELTACORP","DIVISLAB",
    "DIXON","DLF","LALPATHLAB","DRREDDY","EICHERMOT","ESCORTS",
    "EXIDEIND","GAIL","GLENMARK","GMRINFRA","GODREJCP",
    "GODREJPROP","GRANULES","GRASIM","GUJGASLTD","GNFC","HAVELLS",
    "HCLTECH","HDFCAMC","HDFCBANK","HDFCLIFE","HEROMOTOCO",
    "HINDALCO","HAL","HINDCOPPER","HINDPETRO","HINDUNILVR",
    "ICICIBANK","ICICIGI","ICICIPRULI","IDFCFIRSTB","IBULHSGFIN",
    "INDIAMART","IEX","IOC","IRCTC","IGL","INDUSTOWER",
    "INDUSINDBK","NAUKRI","INFY","INTELLECT","INDIGO","IPCALAB",
    "ITC","JINDALSTEL","JKCEMENT","JSWSTEEL","JUBLFOOD",
    "KOTAKBANK","LTTS","LTIM","LT","LAURUSLABS","LICHSGFIN",
    "LUPIN","MGL","M&MFIN","M&M","MANAPPURAM","MARICO","MARUTI",
    "MFSL","METROPOLIS","MOTHERSON","MPHASIS","MRF","MUTHOOTFIN",
    "NATIONALUM","NAVINFLUOR","NESTLEIND","NMDC","NTPC",
    "OBEROIRLTY","ONGC","OFSS","PAGEIND","PERSISTENT","PETRONET",
    "PIIND","PIDILITIND","PEL","POLYCAB","PFC","POWERGRID","PNB",
    "PVRINOX","RAIN","RBLBANK","RECLTD","RELIANCE","SBICARD",
    "SBILIFE","SHREECEM","SHRIRAMFIN","SIEMENS","SRF","SBIN",
    "SAIL","SUNPHARMA","SUNTV","SYNGENE","TATACHEM","TATACOMM",
    "TCS","TATACONSUM","TATAMOTORS","TATAPOWER","TATASTEEL",
    "TECHM","FEDERALBNK","INDIACEM","INDHOTEL","RAMCOCEM","TITAN",
    "TORNTPHARM","TRENT","TVSMOTOR","ULTRACEMCO","UBL","MCDOWELL-N",
    "UPL","VEDL","IDEA","VOLTAS","WHIRLPOOL","WIPRO","ZEEL",
    "ZYDUSLIFE"
]


# ============================================================
# GLOBAL STATE
# ============================================================

smart_api = None
instrument_map = {}

price_volume_data = []
institutional_oi_data = []

pass_state = {}
state_day = None

engine_lock = threading.Lock()


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def today_key():
    return now_ist().strftime("%Y-%m-%d")


def market_status():
    now = now_ist()

    if now.weekday() >= 5:
        return "CLOSED"

    t = now.time()

    if t < datetime.strptime("09:15", "%H:%M").time():
        return "PREMARKET"

    if t <= datetime.strptime("15:30", "%H:%M").time():
        return "OPEN"

    return "CLOSED"


def safe_float(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


# ============================================================
# ANGEL LOGIN
# ============================================================

def login():
    global smart_api

    required = [
        API_KEY,
        CLIENT_CODE,
        PASSWORD,
        TOTP_KEY
    ]

    if not all(required):
        raise RuntimeError(
            "Angel One environment variables are missing."
        )

    smart_api = SmartConnect(api_key=API_KEY)

    totp = pyotp.TOTP(TOTP_KEY).now()

    response = smart_api.generateSession(
        CLIENT_CODE,
        PASSWORD,
        totp
    )

    if not response or not response.get("status"):
        raise RuntimeError(
            f"Angel One login failed: {response}"
        )

    return response


# ============================================================
# INSTRUMENT MASTER
# ============================================================

def load_instrument_master():
    """
    Loads Angel One instrument master and creates an NSE-F&O
    underlying -> representative FUT token mapping.

    This intentionally does NOT hard-code tokens.
    """

    global instrument_map

    url = (
        "https://margincalculator.angelbroking.com/"
        "OpenAPI_File/files/OpenAPIScripMaster.json"
    )

    r = requests.get(url, timeout=30)
    r.raise_for_status()

    data = r.json()

    temp = {}

    for row in data:
        exchange = str(row.get("exch_seg", "")).upper()
        symbol = str(row.get("symbol", ""))
        name = str(row.get("name", "")).upper()
        token = str(row.get("token", ""))

        if exchange != "NFO":
            continue

        if not token:
            continue

        # FUT contracts only
        if not symbol.endswith("FUT"):
            continue

        for stock in FO_STOCKS:
            if name == stock:
                temp[stock] = {
                    "token": token,
                    "symbol": symbol,
                    "name": name,
                    "exchange": "NFO"
                }

    instrument_map = temp

    return {
        "stocks_requested": len(FO_STOCKS),
        "tokens_found": len(instrument_map)
    }


# ============================================================
# ANGEL HISTORICAL DATA
# ============================================================

def get_candles(token, exchange="NFO", interval="FIVE_MINUTE",
                fromdate=None, todate=None):

    if smart_api is None:
        raise RuntimeError("Angel One is not logged in.")

    params = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": fromdate,
        "todate": todate
    }

    response = smart_api.getCandleData(params)

    if not response or not response.get("status"):
        return []

    return response.get("data", []) or []


def candles_to_df(rows):

    if not rows:
        return pd.DataFrame(
            columns=[
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df = df.dropna().sort_values("datetime").reset_index(drop=True)

    return df


# ============================================================
# INDICATORS
# ============================================================

def ema(series, length):
    return series.ewm(
        span=length,
        adjust=False
    ).mean()


def rsi(series, length=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, math.nan)

    return 100 - (100 / (1 + rs))


def atr(df, length=14):

    prev_close = df["close"].shift(1)

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs()
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()


def dmi_adx(df, length=14):

    high = df["high"]
    low = df["low"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = up_move.where(
        (up_move > down_move) & (up_move > 0),
        0
    )

    minus_dm = down_move.where(
        (down_move > up_move) & (down_move > 0),
        0
    )

    a = atr(df, length)

    plus_di = 100 * (
        plus_dm.ewm(alpha=1 / length, adjust=False).mean()
        / a.replace(0, math.nan)
    )

    minus_di = 100 * (
        minus_dm.ewm(alpha=1 / length, adjust=False).mean()
        / a.replace(0, math.nan)
    )

    dx = (
        100 *
        (plus_di - minus_di).abs()
        /
        (plus_di + minus_di).replace(0, math.nan)
    )

    adx = dx.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    return plus_di, minus_di, adx


def vwap(df):

    typical = (
        df["high"] +
        df["low"] +
        df["close"]
    ) / 3

    pv = typical * df["volume"]

    return pv.cumsum() / df["volume"].cumsum()


# ============================================================
# ORIGINAL 20D SAME-TIME CUMULATIVE RVOL
# ============================================================

def same_time_20d_rvol(df):

    """
    Original concept:

    Current-day cumulative volume at the current HH:MM
    divided by the average cumulative volume at the same
    HH:MM across the previous 20 trading days.

    This is NOT a simple rolling volume RVOL.
    """

    if df.empty:
        return 0.0

    d = df.copy()

    d["date"] = d["datetime"].dt.date
    d["hhmm"] = d["datetime"].dt.strftime("%H:%M")

    dates = sorted(d["date"].unique())

    if len(dates) < 2:
        return 0.0

    current_date = dates[-1]

    current_day = d[d["date"] == current_date].copy()

    if current_day.empty:
        return 0.0

    current_hhmm = current_day.iloc[-1]["hhmm"]

    current_slice = current_day[
        current_day["hhmm"] <= current_hhmm
    ]

    current_cum = current_slice["volume"].sum()

    previous_dates = dates[:-1][-20:]

    historical = []

    for day in previous_dates:

        day_df = d[d["date"] == day]

        same_time = day_df[
            day_df["hhmm"] <= current_hhmm
        ]

        if same_time.empty:
            continue

        historical.append(
            same_time["volume"].sum()
        )

    if not historical:
        return 0.0

    avg_hist = sum(historical) / len(historical)

    if avg_hist <= 0:
        return 0.0

    return current_cum / avg_hist


# ============================================================
# SCANNER 1
# ============================================================

def calculate_price_volume(stock, df):

    if df.empty or len(df) < 60:
        return None

    d = df.copy()

    d["ema20"] = ema(d["close"], 20)
    d["ema50"] = ema(d["close"], 50)
    d["rsi"] = rsi(d["close"], 14)

    plus_di, minus_di, adx = dmi_adx(d, 14)

    d["plus_di"] = plus_di
    d["minus_di"] = minus_di
    d["adx"] = adx
    d["vwap"] = vwap(d)

    last = d.iloc[-1]

    c = safe_float(last["close"])
    o = safe_float(last["open"])
    h = safe_float(last["high"])
    l = safe_float(last["low"])

    e20 = safe_float(last["ema20"])
    e50 = safe_float(last["ema50"])
    vw = safe_float(last["vwap"])

    rsi_v = safe_float(last["rsi"])
    adx_v = safe_float(last["adx"])
    pdi = safe_float(last["plus_di"])
    mdi = safe_float(last["minus_di"])

    body = 0.0

    if c > 0:
        body = abs(c - o) / c * 100

    prev_close = safe_float(
        d.iloc[-2]["close"],
        c
    )

    move = 0.0

    if prev_close > 0:
        move = (c - prev_close) / prev_close * 100

    # Current candle volume vs previous candle.
    # This is the acceleration component, separate from RVOL.
    prev_vol = safe_float(
        d.iloc[-2]["volume"],
        0
    )

    curr_vol = safe_float(
        last["volume"],
        0
    )

    volume_accel = (
        curr_vol / prev_vol
        if prev_vol > 0
        else 0.0
    )

    # PDH / PDL from previous trading day
    dates = sorted(d["datetime"].dt.date.unique())

    pdh = 0.0
    pdl = 0.0

    if len(dates) >= 2:

        previous_day = dates[-2]

        prev = d[
            d["datetime"].dt.date == previous_day
        ]

        if not prev.empty:
            pdh = safe_float(prev["high"].max())
            pdl = safe_float(prev["low"].min())

    rvol = same_time_20d_rvol(d)

    near_pdh = (
        pdh > 0 and
        abs(c - pdh) / pdh * 100 <= 0.30
    )

    near_pdl = (
        pdl > 0 and
        abs(c - pdl) / pdl * 100 <= 0.30
    )

    buy_checks = [
        c > e20,
        e20 > e50,
        c > vw,
        rsi_v > 55,
        adx_v > 18,
        pdi > mdi,
        rvol >= 1.10,
        move >= 0.10,
        body >= 0.40
    ]

    sell_checks = [
        c < e20,
        e20 < e50,
        c < vw,
        rsi_v < 45,
        adx_v > 18,
        mdi > pdi,
        rvol >= 1.10,
        move <= -0.10,
        body >= 0.40
    ]

    final_buy = all(buy_checks)
    final_sell = all(sell_checks)

    # Early setup: 7 of 8 concept
    early_buy_checks = [
        rvol >= 1.20,
        volume_accel >= 1.20,
        c > e20,
        c > vw,
        move > 0,
        pdi > mdi,
        adx_v > 18,
        near_pdl
    ]

    early_sell_checks = [
        rvol >= 1.20,
        volume_accel >= 1.20,
        c < e20,
        c < vw,
        move < 0,
        mdi > pdi,
        adx_v > 18,
        near_pdh
    ]

    early_buy = sum(early_buy_checks) >= 7
    early_sell = sum(early_sell_checks) >= 7

    # Final has priority.
    if final_buy:
        side = "BUY"
        signal_type = "FINAL"
    elif final_sell:
        side = "SELL"
        signal_type = "FINAL"
    elif early_buy:
        side = "BUY"
        signal_type = "EARLY"
    elif early_sell:
        side = "SELL"
        signal_type = "EARLY"
    else:
        return None

    now = now_ist()

    day = now.strftime("%Y-%m-%d")

    # Daily reset
    if state_day != day:
        pass_state.clear()

    key = stock

    if key not in pass_state:

        pass_state[key] = {
            "side": side,
            "signal_type": signal_type,
            "passTime": now.strftime("%H:%M")
        }

    locked = pass_state[key]

    level = ""

    if pdh and c >= pdh * (1 - 0.003):
        level = "PDH"

    if pdl and c <= pdl * (1 + 0.003):
        level = "PDL"

    return {
        "scanner": "booster",
        "symbol": stock,
        "ltp": round(c, 2),
        "rvol": round(rvol, 4),
        "volumeAccel": round(volume_accel, 4),
        "vwap": round(vw, 2),
        "ema20": round(e20, 2),
        "ema50": round(e50, 2),
        "pdh": round(pdh, 2),
        "pdl": round(pdl, 2),
        "level": level,
        "momentum": round(move, 4),
        "side": locked["side"],
        "signalType": locked["signal_type"],
        "passTime": locked["passTime"]
    }


# ============================================================
# SCANNER 2 — INDEPENDENT PRICE + OI
# ============================================================

def classify_oi(price_change, oi_change):

    if price_change > 0 and oi_change > 0:
        return "LONG BUILDUP", "BUY"

    if price_change < 0 and oi_change > 0:
        return "SHORT BUILDUP", "SELL"

    if price_change > 0 and oi_change < 0:
        return "SHORT COVERING", "BUY"

    if price_change < 0 and oi_change < 0:
        return "LONG UNWINDING", "SELL"

    return "NEUTRAL", ""


def calculate_flow(stock, df, oi_current, oi_previous):

    if df.empty:
        return None

    c = safe_float(df.iloc[-1]["close"])

    previous_close = (
        safe_float(df.iloc[-2]["close"])
        if len(df) >= 2
        else c
    )

    if previous_close <= 0:
        price_change = 0.0
    else:
        price_change = (
            (c - previous_close)
            / previous_close
        ) * 100

    oi_current = safe_float(oi_current)
    oi_previous = safe_float(oi_previous)

    if oi_previous > 0:
        oi_change = (
            (oi_current - oi_previous)
            / oi_previous
        ) * 100
    else:
        oi_change = 0.0

    flow_type, side = classify_oi(
        price_change,
        oi_change
    )

    if flow_type == "NEUTRAL":
        return None

    confirmation = "PRICE + OI"

    return {
        "scanner": "flow",
        "symbol": stock,
        "ltp": round(c, 2),
        "oi": int(oi_current),
        "oiChange": round(oi_change, 2),
        "priceChange": round(price_change, 2),
        "volume": int(
            safe_float(df.iloc[-1]["volume"])
        ),
        "type": flow_type,
        "confirmation": confirmation,
        "rank": 0,
        "level": "",
        "side": side
    }


# ============================================================
# LIVE ENGINE
# ============================================================

def engine_cycle():

    global price_volume_data
    global institutional_oi_data
    global state_day

    with engine_lock:

        if market_status() != "OPEN":
            return

        if smart_api is None:
            return

        today = today_key()

        if state_day != today:
            pass_state.clear()
            state_day = today

        new_price = []
        new_flow = []

        # ----------------------------------------------------
        # NOTE:
        # Angel One historical candles provide OHLCV.
        # OI retrieval depends on the exact SmartAPI endpoint/
        # contract response available to the account.
        #
        # Therefore OI is kept as a separate input path.
        # No Scanner 1 RVOL is reused for Scanner 2.
        # ----------------------------------------------------

        for stock in FO_STOCKS:

            token_info = instrument_map.get(stock)

            if not token_info:
                continue

            token = token_info["token"]

            try:

                now = now_ist()

                from_dt = (
                    now - timedelta(days=35)
                ).replace(
                    hour=9,
                    minute=15,
                    second=0,
                    microsecond=0
                )

                rows = get_candles(
                    token=token,
                    exchange="NFO",
                    interval="FIVE_MINUTE",
                    fromdate=from_dt.strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    todate=now.strftime(
                        "%Y-%m-%d %H:%M"
                    )
                )

                df = candles_to_df(rows)

                result = calculate_price_volume(
                    stock,
                    df
                )

                if result:
                    new_price.append(result)

            except Exception as e:
                print(
                    f"Scanner 1 error {stock}: {e}"
                )

            # Scanner 2 OI intentionally remains separate.
            #
            # Do NOT manufacture OI from volume.
            # Do NOT reuse Scanner 1 RVOL.
            #
            # When the exact live OI feed is connected,
            # calculate_flow() receives:
            #
            # oi_current
            # oi_previous
            #
            # and creates the independent flow result.

        # ----------------------------------------------------
        # Rank institutional flow independently
        # ----------------------------------------------------

        new_flow.sort(
            key=lambda x: (
                abs(
                    safe_float(x.get("oiChange"))
                ) +
                abs(
                    safe_float(x.get("priceChange"))
                )
            ),
            reverse=True
        )

        for i, item in enumerate(new_flow, 1):
            item["rank"] = i

        price_volume_data = new_price
        institutional_oi_data = new_flow


def background_loop():

    while True:

        try:
            engine_cycle()

        except Exception as e:
            print(
                "ENGINE ERROR:",
                repr(e)
            )

        # 5-minute aligned-ish refresh
        time.sleep(20)


# ============================================================
# API ROUTES
# ============================================================

@app.get("/")
def root():

    return {
        "app": "F&O SCANNER",
        "status": "LIVE ENGINE",
        "market": market_status(),
        "time": now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "scanner1": "Price + Volume",
        "scanner2": "Independent Price + OI",
        "double": "Cross Confirmation",
        "fake_data": False
    }


@app.get("/health")
def health():

    return {
        "ok": True,
        "market": market_status(),
        "angel_logged_in": smart_api is not None,
        "tokens": len(instrument_map),
        "time": now_ist().isoformat()
    }


@app.get("/api/scanner/booster")
def scanner_booster():

    return {
        "scanner": "booster",
        "time": now_ist().isoformat(),
        "data": price_volume_data
    }


@app.get("/api/scanner/flow")
def scanner_flow():

    return {
        "scanner": "flow",
        "time": now_ist().isoformat(),
        "data": institutional_oi_data
    }


@app.get("/api/scanner/double")
def scanner_double():

    booster = {
        x["symbol"]: x
        for x in price_volume_data
    }

    flow = {
        x["symbol"]: x
        for x in institutional_oi_data
    }

    result = []

    for symbol, b in booster.items():

        f = flow.get(symbol)

        if not f:
            continue

        buy_match = (
            b["side"] == "BUY"
            and f["type"] in [
                "LONG BUILDUP",
                "SHORT COVERING"
            ]
        )

        sell_match = (
            b["side"] == "SELL"
            and f["type"] in [
                "SHORT BUILDUP",
                "LONG UNWINDING"
            ]
        )

        if buy_match or sell_match:

            result.append({
                "symbol": symbol,
                "side": b["side"],
                "booster": b,
                "flow": f
            })

    return {
        "scanner": "double",
        "time": now_ist().isoformat(),
        "data": result
    }


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup():

    global smart_api

    try:

        login()

        print("Angel One login: OK")

        info = load_instrument_master()

        print(
            "Instrument master:",
            info
        )

        thread = threading.Thread(
            target=background_loop,
            daemon=True
        )

        thread.start()

        print(
            "F&O Scanner live engine started."
        )

    except Exception as e:

        smart_api = None

        print(
            "Startup warning:",
            repr(e)
        )

