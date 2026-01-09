#!/usr/bin/env python3
# expiry_niftyft_701.py (Flattrade + Broker MTM, robust manual-exit confirmation, neutrality, closed-entries capture)
# MTM is sourced from Flattrade PositionBook (rpnl + urmtom for this strategy's symbols).
# Includes strict manual exit confirmation, forced EOD exit for any broker-open legs, and closed-entry rpnl capture.
# Persists closed rpnl across the day so Strategy MTM always includes it, even if broker drops closed rows.
# Aligns NIFTY position rows and CE/PE change lines for readability (keeps PCT/ATM header unchanged).
# Ensures only a single consolidated row per instrument (tsym) is displayed — no duplicate open/closed rows.
# Adds optional PCT-based watch windows:
#   - PCT Start window: watch from configured time; if CE/PE ATM diff% >= threshold, allow early entry before START_TIME
#     else enter at normal START_TIME.
#   - PCT End window: watch from configured time; if CE/PE ATM diff% <= threshold, gracefully exit for the day regardless of EXIT_TIME.
# Preserves all existing strategy logic, features, and comments as-is.

import requests
import datetime
import time
import re
from copy import deepcopy
from typing import Dict, Any, Optional, List, Tuple
import os
import json

# =====================
# USER CONFIG — EDIT THESE VALUES
# =====================

# Strategy contexts (percent triggers)
STRATEGIES = [
    (15.0, "unused://flattrade"),
]

# Symbols, expiry and quantities
SYMBOL = "NIFTY"
EXPIRY_DATE = "2025-12-04"   # ISO yyyy-mm-dd
STRADDLE_LOTS = 1            # lots to trade per entry (per leg)
LOT_SIZE_MAP = {"NIFTY": 65} # units per lot

# Time windows (core)
START_TIME_STR = "11:15"     # HH:MM 24h
EXIT_TIME_STR  = "15:24"     # HH:MM 24h

# Optional PCT watch windows (leave empty strings to disable and use normal start/end times)
# Example: PCT_START_TIME_STR="09:16", PCT_START_DIFF=30.0
#          PCT_END_TIME_STR="14:45",   PCT_END_DIFF=1.0
PCT_START_TIME_STR = "10:45"       # HH:MM 24h; "" disables start watch
PCT_END_TIME_STR   = "14:50"       # HH:MM 24h; "" disables end watch
PCT_START_DIFF     = 10.0     # enter early when ATM CE vs PE diff% >= this
PCT_END_DIFF       = 1.0      # exit early when ATM CE vs PE diff% <= this

# Strategy flags
REFRESH_INTERVAL = 10        # seconds
HOLD_ENABLED = False
HOLD_TIME_MINUTES = 1
REQUIRE_ATM_CHANGE = True

# Stop/Target thresholds (per-lot)
STOPLOSS_PER_LOT = 2000.0
TARGET_PER_LOT   = 6000.0

# If True, stoploss/target are applied to TOTAL MTM (account-wide).
# If False (default), stoploss/target apply to STRATEGY MTM (running + closed positions of the strategy scope).
STOP_TARGET_ON_TOTAL = False

# If True (default), when the script starts it will "baseline" existing PositionBook values
# and compute Strategy MTM as the change since script start (fresh MTM). This prevents previously
# closed P&L from terminating the strategy immediately. If False, strategy MTM will include historical closed positions.
IGNORE_PAST_STRATEGY_POSITIONS = True

# Logging style
LOG_STYLE = "classic"        # "classic" or "single"
SHOW_MTM_ROWS = True         # True: show per-symbol netqty|rpnl|urmtom rows

# IMPORTANT: isolate MTM/logging to only OUR strategy orders (ignore other strategies)
ISOLATE_BY_OUR_ORDERS = True

# Capture closed entries and include their rpnl in Strategy MTM (broker-truth, product-restricted)
CAPTURE_CLOSED_ENTRIES = True
CAPTURE_CLOSED_RPNL_IN_STRATEGY = True

# Re-entry and retry limits
MAX_REENTRY_ATTEMPTS = 3          # Max times to re-enter after manual/neutral exit in a day
REENTRY_COOLDOWN_SECONDS = 60     # Cooldown between re-entry attempts
MAX_ORDER_RETRY_ATTEMPTS = 5      # Max broker retries for a single buy/sell/straddle action
ORDER_RETRY_BACKOFF_SECONDS = 3   # Backoff between broker retries

# Manual exit confirmation (to avoid false positives from transient PB gaps)
MANUAL_EXIT_CONFIRM_SAMPLES = 2            # consecutive PB reads that must show netqty=0
MANUAL_EXIT_CONFIRM_WINDOW_SECONDS = 1.5   # total window across the confirmation samples

# Flattrade credentials (update JKEY daily)
FT_UID   = "FZ21701"
FT_ACTID = "FZ21701"
FT_JKEY  = ""

# override FT_UID/FT_ACTID/FT_JKEY from flattrade token file if available
try:
    _tf = os.getenv("FLATTRADE_TOKEN_FILE", "/home/opc/algo/flattrade/test/Nifty/flattrade_token_headless.json")
    with open(_tf, "r", encoding="utf-8") as _f:
        _d = json.load(_f)
    _tr = _d.get("token_response", _d) if isinstance(_d, dict) else {}
    _token = _tr.get("token") or _tr.get("apitoken") or _tr.get("jkey")
    _client = _tr.get("client") or _tr.get("client_id")
    if _client:
        FT_UID = FT_ACTID = _client
    if _token:
        FT_JKEY = _token
except Exception:
    pass

# Upstox option chain access
UPSTOX_ACCESS_TOKEN = ""   # paste token if you want to override

# Static app credentials (not overridden by token file)
UPSTOX_CLIENT_ID     = "21b2f8e5-fc84-45cc-9e6e-4e0adf2e9160"
UPSTOX_CLIENT_SECRET = "6d5gyos6w3"
UPSTOX_TOKEN_URL     = "https://api.upstox.com/v2/login/authorization/token"
# UPSTOX_REFRESH_TOKEN = ""  # (not issued yet; add when available)

# Load token from file only if manual token left blank
if not UPSTOX_ACCESS_TOKEN:
    try:
        path = os.getenv("UPSTOX_TOKEN_FILE", "/home/opc/algo/flattrade/test/Nifty/upstox_token.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        UPSTOX_ACCESS_TOKEN = data.get("access_token", "")
    except Exception:
        pass  # keep empty; strategy can decide what to do

def upstox_auth_header():
    return {"Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}"} if UPSTOX_ACCESS_TOKEN else {}

# =====================
# DERIVED/CONSTANTS
# =====================

def _parse_hhmm(s: str) -> datetime.time:
    try:
        h, m = s.strip().split(":")
        return datetime.time(int(h), int(m))
    except Exception:
        return datetime.time(9, 15)

START_TIME = _parse_hhmm(START_TIME_STR)
EXIT_TIME  = _parse_hhmm(EXIT_TIME_STR)

def _parse_optional_hhmm(s: str) -> Optional[datetime.time]:
    try:
        s = (s or "").strip()
        if not s:
            return None
        h, m = s.split(":")
        return datetime.time(int(h), int(m))
    except Exception:
        return None

PCT_START_TIME = _parse_optional_hhmm(PCT_START_TIME_STR)
PCT_END_TIME   = _parse_optional_hhmm(PCT_END_TIME_STR)

HOLD_TIME = datetime.timedelta(minutes=HOLD_TIME_MINUTES)

UPSTOX_URL = "https://api.upstox.com/v2/option/chain"
UPSTOX_PARAMS_BASE = {"instrument_key": "NSE_INDEX|Nifty 50"}

FLATTRADE_BASE_URL = "https://piconnect.flattrade.in/PiConnectTP"
FLATTRADE_EXCHANGE = "NFO"                  # NIFTY options
FLATTRADE_PRODUCT  = "M"                    # "I"=MIS, "M"=NRML

# Selected expiry (dynamic) will be stored here (initially fallback)
SELECTED_EXPIRY = EXPIRY_DATE

# =====================
# GLOBAL: baseline map captured at script start when IGNORE_PAST_STRATEGY_POSITIONS=True
# Maps tsym_upper -> baseline_value (rpnl + urmtom_when_open OR rpnl if closed)
# =====================
BASELINE_PB_MAP: Dict[str, float] = {}

# =====================
# Margin detection helpers
# =====================
_MARGIN_KEYWORDS = [
    "margin", "shortfall", "insufficient", "insufficient funds",
    "not enough margin", "not enough funds", "funds insufficient",
    "check cash ratio", "cash ratio"
]

def _extract_broker_message(resp_or_text) -> str:
    try:
        if isinstance(resp_or_text, dict):
            for k in ("emsg", "message", "error", "err", "stat", "status"):
                v = resp_or_text.get(k)
                if v:
                    return str(v)
            return str(resp_or_text)
        else:
            return str(resp_or_text)
    except Exception:
        return str(resp_or_text)

def _is_margin_error(resp_or_text) -> bool:
    txt = _extract_broker_message(resp_or_text).lower()
    for kw in _MARGIN_KEYWORDS:
        if kw in txt:
            return True
    return False

# =====================
# UTIL
# =====================

def nowstr() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")

def nowstr_full() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

def get_lot_size(symbol: str) -> int:
    return LOT_SIZE_MAP.get(symbol.upper(), 1)

# =====================
# FILE LOGGING
# =====================

def get_logfile_path():
    logdir = os.path.join(os.getcwd(), "logs")
    os.makedirs(logdir, exist_ok=True)
    now = datetime.datetime.now()
    try:
        expiry_dd = datetime.datetime.strptime(EXPIRY_DATE, "%Y-%m-%d").strftime("%d")
    except Exception:
        expiry_dd = "XX"
    fname = f"{SYMBOL}_{now.strftime('%H%M%S_%d%m%Y')}_({expiry_dd}).log"
    return os.path.join(logdir, fname)

def write_logfile_entry(logpath, entry: str):
    try:
        with open(logpath, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        print(f"[{nowstr()}] ⚠️ Could not write to log file: {e}")

# =====================
# FLATTRADE TRANSPORT (form-encoded jData JSON + jKey)
# =====================

def ft_post(endpoint: str, jdata: dict) -> dict:
    import json as pyjson
    url = f"{FLATTRADE_BASE_URL}/{endpoint}"
    body = f'jData={pyjson.dumps(jdata, separators=(",", ":"))}&jKey={FT_JKEY}'
    try:
        resp = requests.post(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        ctype = (resp.headers.get("Content-Type") or "").lower()
        return resp.json() if ("application/json" in ctype or resp.text.strip().startswith("{")) else {"stat": "Not_Ok", "emsg": resp.text}
    except Exception as e:
        return {"stat": "Not_Ok", "emsg": str(e)}

def flattrade_place_order(tsym: str, qty_units: int, trantype: str, prctyp: str = "MKT", prc: Optional[float] = None, label: str = "") -> dict:
    jdata = {
        "uid": FT_UID,
        "actid": FT_ACTID,
        "exch": FLATTRADE_EXCHANGE,
        "tsym": tsym,
        "qty": str(int(qty_units)),
        "prc": "0" if prctyp == "MKT" or prc is None else str(prc),
        "prd": FLATTRADE_PRODUCT,
        "trantype": trantype,
        "prctyp": prctyp,
        "ret": "DAY",
        "ordersource": "API",
    }
    resp = ft_post("PlaceOrder", jdata)
    print(f"[{nowstr()}] 🔹 FT {label} | {trantype} {tsym} x {qty_units} | resp={resp}")
    return resp

def flattrade_fetch_positionbook() -> List[dict]:
    jdata = {"uid": FT_UID, "actid": FT_ACTID}
    resp = ft_post("PositionBook", jdata)
    return resp if isinstance(resp, list) else []

def ft_get_order_book() -> List[dict]:
    jdata = {"uid": FT_UID, "actid": FT_ACTID}
    resp = ft_post("OrderBook", jdata)
    return resp if isinstance(resp, list) else []

def ft_get_trade_book() -> List[dict]:
    jdata = {"uid": FT_UID, "actid": FT_ACTID}
    resp = ft_post("TradeBook", jdata)
    return resp if isinstance(resp, list) else []

# =====================
# SYMBOL HELPERS (internal symbol ↔ Flattrade tsym)
# =====================

def expiry_to_yymmdd(raw_expiry: str) -> str:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(raw_expiry, fmt)
            return dt.strftime("%y%m%d")
        except Exception:
            continue
    try:
        dt = datetime.datetime.fromisoformat(raw_expiry)
        return dt.strftime("%y%m%d")
    except Exception:
        return raw_expiry

def build_option_symbol(symbol: str, expiry_raw: str, strike: int, opt_type: str) -> str:
    yymmdd = expiry_to_yymmdd(expiry_raw)
    strike_str = str(int(strike))
    return f"{symbol}{yymmdd}{opt_type}{strike_str}"

def extract_strike_from_symbol(instrument: str) -> Optional[int]:
    m = re.search(r'^([A-Z]+)(\d{6})([CP])(\d+)$', instrument)
    if not m:
        return None
    try:
        return int(m.group(4))
    except Exception:
        return None

def instrument_opt_type(instrument: str) -> Optional[str]:
    m = re.search(r'^([A-Z]+)(\d{6})([CP])(\d+)$', instrument)
    if not m:
        return None
    return m.group(3)  # 'C' or 'P'

def tsym_from_internal(instrument: str) -> str:
    """
    Convert internal instrument (e.g., NIFTYyymmddC26150) to Flattrade tsym NIFTYddMONyyC26150 using embedded yymmdd.
    """
    m = re.fullmatch(r'^([A-Z]+)(\d{6})([CP])(\d+)$', instrument)
    if not m:
        return instrument
    sym, yymmdd, opt, strike = m.groups()
    try:
        dt = datetime.datetime.strptime(yymmdd, "%y%m%d")
    except Exception:
        try:
            dt = datetime.datetime.fromisoformat(EXPIRY_DATE)
        except Exception:
            dt = datetime.datetime.utcnow()
    ddmonyy = dt.strftime("%d%b%y").upper()
    return f"{sym}{ddmonyy}{opt}{int(strike)}"

def parse_ft_tsym_to_internal(tsym: str, expiry_yymmdd: str) -> Optional[str]:
    try:
        ts = str(tsym).upper()
        if not ts.startswith(SYMBOL.upper()):
            return None
        m = re.search(r'(?P<strike>\d{3,6})(?P<op>CE|PE)$', ts)
        if not m:
            return None
        strike = int(m.group("strike"))
        op_one = "C" if m.group("op") == "CE" else "P"
        return f"{SYMBOL}{expiry_yymmdd}{op_one}{strike}"
    except Exception:
        return None

# =====================
# UPSTOX HELPERS (option chain)
# =====================

def upstox_refresh_access_token() -> Optional[str]:
    if not (UPSTOX_CLIENT_ID and UPSTOX_CLIENT_SECRET and os.getenv("UPSTOX_REFRESH_TOKEN")):
        print(f"[{nowstr()}] ⚠️ Upstox refresh skipped: missing client/secret/refresh_token")
        return None
    data = {
        "grant_type": "refresh_token",
        "client_id": UPSTOX_CLIENT_ID,
        "client_secret": UPSTOX_CLIENT_SECRET,
        "refresh_token": os.getenv("UPSTOX_REFRESH_TOKEN"),
    }
    try:
        r = requests.post(UPSTOX_TOKEN_URL, data=data, timeout=15)
    except Exception as e:
        print(f"[{nowstr()}] ⚠️ Upstox refresh error: {e}")
        return None
    if r.status_code != 200:
        print(f"[{nowstr()}] ⚠️ Upstox refresh HTTP {r.status_code}: {r.text[:400]}")
        return None
    try:
        payload = r.json()
    except Exception:
        print(f"[{nowstr()}] ⚠️ Upstox non-JSON payload: {r.text[:200]}")
        return None
    access_token = payload.get("access_token")
    if not access_token:
        print(f"[{nowstr()}] ⚠️ Upstox refresh returned no access_token: {payload}")
        return None
    print(f"[{nowstr()}] 🔄 Upstox access token refreshed.")
    return access_token

def get_option_chain_from_upstox(expiry_date: str) -> List[dict]:
    params = deepcopy(UPSTOX_PARAMS_BASE)
    params["expiry_date"] = expiry_date

    def do_call(tok: Optional[str]) -> requests.Response:
        headers = {"Accept": "application/json"}
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        return requests.get(UPSTOX_URL, params=params, headers=headers, timeout=10)

    tok = UPSTOX_ACCESS_TOKEN or None
    try:
        resp = do_call(tok)
    except Exception as e:
        print(f"[{nowstr()}] ⚠️ Upstox connection error: {e}")
        return []

    if resp.status_code == 401:
        print(f"[{nowstr()}] ⚠️ Upstox 401: attempting token refresh...")
        new_tok = upstox_refresh_access_token()
        if new_tok:
            try:
                resp = do_call(new_tok)
            except Exception as e:
                print(f"[{nowstr()}] ⚠️ Upstox connection error after refresh: {e}")
                return []
        else:
            return []

    if resp.status_code != 200:
        print(f"[{nowstr()}] ⚠️ Upstox returned {resp.status_code}: {resp.text[:400]}")
        return []

    try:
        payload = resp.json()
    except Exception:
        print(f"[{nowstr()}] ⚠️ Upstox non-JSON payload: {resp.text[:200]}")
        return []
    return payload.get("data", payload)

def pick_atm_from_chain(data) -> (float, Optional[int], Optional[dict]):
    best_item = None
    min_diff = float("inf")
    spot_price = 0.0
    for item in data:
        strike = item.get("strike_price") or item.get("strike")
        if strike is None: continue
        try:
            ce_ltp = float(item.get("call_options", {}).get("market_data", {}).get("ltp") or 0.0)
            pe_ltp = float(item.get("put_options", {}).get("market_data", {}).get("ltp") or 0.0)
        except Exception:
            ce_ltp = pe_ltp = 0.0
        try:
            spot_price = float(item.get("underlying_spot_price", spot_price or 0))
        except Exception:
            pass
        if ce_ltp <= 0.0 or pe_ltp <= 0.0:
            continue
        diff = abs(ce_ltp - pe_ltp)
        if diff < min_diff:
            min_diff = diff
            best_item = item
    if best_item is not None:
        strike_val = best_item.get("strike_price") or best_item.get("strike")
        try:
            return spot_price, int(strike_val), best_item
        except Exception:
            return spot_price, strike_val, best_item
    # Fallback: nearest to spot
    fallback_item = None
    min_dist = float("inf")
    for item in data:
        strike = item.get("strike_price") or item.get("strike")
        if strike is None: continue
        try:
            sp = float(item.get("underlying_spot_price", spot_price or 0))
        except Exception:
            sp = spot_price or 0.0
        try:
            strike_f = float(strike)
        except Exception:
            continue
        dist = abs(sp - strike_f)
        if dist < min_dist:
            min_dist = dist
            fallback_item = item
            spot_price = sp
    if fallback_item is not None:
        strike_val = fallback_item.get("strike_price") or fallback_item.get("strike")
        try:
            return spot_price, int(strike_val), fallback_item
        except Exception:
            return spot_price, strike_val, fallback_item
    return spot_price, None, None

def get_leg_ltp_from_chain(data, strike, opt_type):
    if strike is None:
        return 0.0
    for item in data:
        s = item.get("strike_price") or item.get("strike")
        if s is None: continue
        try: s = int(float(s))
        except Exception: continue
        if s != strike: continue
        md = item.get("call_options", {}).get("market_data", {}) if opt_type == "C" else item.get("put_options", {}).get("market_data", {})
        try:
            ltp = float(md.get("ltp", 0.0))
        except Exception:
            ltp = 0.0
        return ltp
    return 0.0

def find_nearest_expiry_by_probe(max_days: int = 14) -> Optional[str]:
    today = datetime.date.today()
    for i in range(0, max_days + 1):
        d = today + datetime.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        expiry = d.strftime("%Y-%m-%d")
        try:
            data = get_option_chain_from_upstox(expiry)
            if isinstance(data, list) and len(data) > 0:
                return expiry
        except Exception:
            continue
    return None

def compute_atm_cepe_diff_pct(chain_data: List[dict], atm_strike: Optional[int]) -> Optional[float]:
    """
    Compute symmetric percent difference between ATM CE and PE LTP:
      diff_pct = |CE - PE| / ((CE + PE)/2) * 100
    Returns None if LTPs invalid.
    """
    try:
        if atm_strike is None:
            return None
        ce = float(get_leg_ltp_from_chain(chain_data, atm_strike, "C"))
        pe = float(get_leg_ltp_from_chain(chain_data, atm_strike, "P"))
        if ce <= 0.0 or pe <= 0.0:
            return None
        avg = (ce + pe) / 2.0
        if avg <= 0.0:
            return None
        return abs(ce - pe) / avg * 100.0
    except Exception:
        return None

# =====================
# MTM / STRATEGY HELPERS
# =====================

def _pb_rows_for_tsyms(tsyms: List[str], product: Optional[str]) -> Dict[str, dict]:
    """
    Build a map tsym_upper -> broker PositionBook row (restricted by product if provided).
    """
    out: Dict[str, dict] = {}
    pb = flattrade_fetch_positionbook()
    if not pb:
        return out
    targ = {str(t).upper() for t in tsyms if t}
    for r in pb:
        try:
            ts = str(r.get("tsym", "")).upper()
            if ts not in targ:
                continue
            prd_val = str(r.get("prd") or r.get("product") or "").upper()
            if product and prd_val and prd_val != str(product).upper():
                continue
            out[ts] = r
        except Exception:
            continue
    return out

def _detect_option_type_from_tsym(ts: str) -> Optional[str]:
    if not ts:
        return None
    tsu = str(ts).upper()
    m = re.search(r'(CE|PE)$', tsu)
    if m:
        return "C" if m.group(1) == "CE" else "P"
    m2 = re.search(r'(C|P)\d{3,6}$', tsu)
    if m2:
        return m2.group(1)
    return None

def _sum_netqty_from_tradebook(tsym_upper: str, product: Optional[str] = FLATTRADE_PRODUCT) -> int:
    """
    Compute netqty from TradeBook (account-wide), restricted by tsym and product if available.
    """
    try:
        tb = ft_get_trade_book()
        if not tb:
            return 0
        net = 0
        for t in tb:
            try:
                ts = str(t.get("tsym", "")).upper()
                if ts != tsym_upper:
                    continue
                prd_val = str(t.get("prd") or t.get("product") or "").upper()
                if product and prd_val and prd_val != str(product).upper():
                    continue
                trantype = str(t.get("trantype", "")).upper()
                qty = int(float(t.get("qty") or t.get("fillshares") or t.get("fillQty") or 0))
                if trantype == "B":
                    net += qty
                elif trantype == "S":
                    net -= qty
            except Exception:
                continue
        return net
    except Exception:
        return 0

def _broker_netqty_for_tsym_product(tsym: Optional[str], product: Optional[str] = FLATTRADE_PRODUCT) -> int:
    """
    Broker-truth netqty for a given tsym restricted to product type if available.
    Prevents accidental 'exit' orders opening new longs when already flat.
    """
    try:
        if not tsym:
            return 0
        ts_target = str(tsym).upper()
        pb = flattrade_fetch_positionbook()
        if not pb:
            return 0
        for r in pb:
            try:
                ts = str(r.get("tsym", "")).upper()
                if ts != ts_target:
                    continue
                prd_val = str(r.get("prd") or r.get("product") or "").upper()
                if product and prd_val and prd_val != str(product).upper():
                    continue
                return int(float(r.get("netqty", 0) or 0))
            except Exception:
                continue
        return 0
    except Exception:
        return 0

def _broker_row_for_tsym_product(tsym: Optional[str], product: Optional[str] = FLATTRADE_PRODUCT) -> Optional[dict]:
    if not tsym:
        return None
    rows = _pb_rows_for_tsyms([tsym], product)
    return rows.get(str(tsym).upper())

def _get_posrow_for_tsym(tsym_upper: str, product: Optional[str] = FLATTRADE_PRODUCT) -> Optional[dict]:
    try:
        pb = flattrade_fetch_positionbook()
        if not pb:
            return None
        for r in pb:
            try:
                ts = str(r.get("tsym", "")).upper()
                if ts != str(tsym_upper).upper():
                    continue
                prd_val = str(r.get("prd") or r.get("product") or "").upper()
                if product and prd_val and prd_val != str(product).upper():
                    continue
                return r
            except Exception:
                continue
    except Exception:
        pass
    return None

def _get_ltp_from_posrow_or_quote(posrow: Optional[dict]) -> Optional[float]:
    try:
        if not posrow:
            return None
        lp = posrow.get("lp") or posrow.get("last_price") or posrow.get("ltp")
        if lp:
            return float(lp)
        token = posrow.get("token")
        exch = posrow.get("exch", FLATTRADE_EXCHANGE)
        uid = FT_UID
        if token:
            jdata = {"uid": uid, "exch": exch, "token": str(token)}
            resp = ft_post("GetQuotes", jdata)
            if isinstance(resp, dict):
                lp2 = resp.get("lp") or resp.get("last_price") or resp.get("ltp")
                if not lp2 and "values" in resp and isinstance(resp["values"], list) and resp["values"]:
                    lp2 = resp["values"][0].get("lp") or resp["values"][0].get("ltp")
                if lp2:
                    return float(lp2)
    except Exception:
        pass
    return None

# =====================
# BROKER SEND HELPERS (Flattrade)
# =====================

def flattrade_response_to_tuple(resp: dict) -> Tuple[bool, Optional[int], str, dict]:
    ok = isinstance(resp, dict) and resp.get("stat") == "Ok"
    text = ""
    if isinstance(resp, dict):
        for k in ("emsg", "message", "error", "err"):
            if resp.get(k):
                text = str(resp.get(k))
                break
    if not text:
        text = "" if ok else (resp.get("emsg") if isinstance(resp, dict) else str(resp))
    return ok, None, text or "", resp

def _extract_order_id_from_resp(raw: dict) -> Optional[str]:
    try:
        oid = raw.get("norenordno") or raw.get("orderid")
        return str(oid) if oid else None
    except Exception:
        return None

def broker_send_sell(_unused_webhook_url: str, instrument: str, lots: int, label: str):
    tsym = tsym_from_internal(instrument)
    qty_units = max(1, int(lots) * get_lot_size(SYMBOL))
    resp = flattrade_place_order(tsym=tsym, qty_units=qty_units, trantype="S", prctyp="MKT", prc=None, label=label)
    ok, status, text, raw = flattrade_response_to_tuple(resp)
    oid_entry = _extract_order_id_from_resp(raw or {})
    return (ok, status, text, raw), tsym

def broker_send_buy(_unused_webhook_url: str, instrument: str, lots: int, label: str):
    tsym = tsym_from_internal(instrument)
    qty_units = max(1, int(lots) * get_lot_size(SYMBOL))
    resp = flattrade_place_order(tsym=tsym, qty_units=qty_units, trantype="B", prctyp="MKT", prc=None, label=label)
    ok, status, text, raw = flattrade_response_to_tuple(resp)
    oid_exit = _extract_order_id_from_resp(raw or {})
    return (ok, status, text, raw), tsym

# =====================
# LOGGING HELPERS
# =====================

def log_atm_line(ctx_pct: float, spot: float, atm: int, ce_strike: Optional[int], ce_ltp: float, pe_strike: Optional[int], pe_ltp: float, expiry: str) -> str:
    return f"[{nowstr()}] [PCT={ctx_pct:g}%] 📊 Spot={spot:.2f} | ATM={atm} | CE ATM={ce_strike} | CE={ce_ltp:.2f} | PE ATM={pe_strike} | PE={pe_ltp:.2f} | Expiry={expiry}"

# Fixed column widths for aligned printing (tweak if needed)
COL_TS      = 24   # "💰 SYMBOL"
COL_NET     = 14   # "netqty=..."
COL_ENTRY   = 24   # "CE/PE Entry LTP= ..."
COL_CURR    = 24   # "CE/PE Current LTP= ..."
COL_RPNL    = 14   # "rpnl=..."
COL_UR      = 14   # "urmtom=..."

def _fmt_ltp(val: Optional[float]) -> str:
    return f"{val:.2f}" if (val is not None) else "__"

def _align_join(parts: List[str], widths: List[int]) -> str:
    return " | ".join(p.ljust(w) for p, w in zip(parts, widths))

def log_mtm_row_aligned(ts: str, netqty: int, rpnl: float, ur: float, entry_ltp: Optional[float] = None, current_ltp: Optional[float] = None) -> str:
    opt = _detect_option_type_from_tsym(ts) or ""
    leg_lbl = "CE" if opt == "C" else ("PE" if opt == "P" else "")
    parts = [
        f"💰 {ts}",
        f"netqty={netqty}",
        f"{leg_lbl} Entry LTP= {_fmt_ltp(entry_ltp)}",
        f"{leg_lbl} Current LTP= {_fmt_ltp(current_ltp)}",
        f"rpnl={rpnl:.2f}",
        f"urmtom={ur:.2f}",
    ]
    widths = [COL_TS, COL_NET, COL_ENTRY, COL_CURR, COL_RPNL, COL_UR]
    return _align_join(parts, widths)

def log_change_line_aligned(ce_change: float, pe_change: float) -> str:
    left = f"🔍 CE Change={ce_change:+.2f}%"
    left_padded = left.ljust(COL_TS)
    return f"{left_padded} | PE Change={pe_change:+.2f}%"

def log_mtm_total(ctx_pct: float, total: float, rpnl: float, ur: float) -> str:
    return f"[{nowstr()}] [PCT={ctx_pct:g}%] 💰 MTM={total:.2f} (rpnl={rpnl:.2f}, urmtom={ur:.2f})"

# =====================
# STRATEGY CONTEXT
# =====================

class StrategyContext:
    def __init__(self, trigger_pct: float, webhook_url: str):
        self.trigger_pct = trigger_pct
        self.webhook_url = webhook_url
        self.in_position: bool = False
        self.ce_strike: Optional[int] = None
        self.ce_entry_price: Optional[float] = None
        self.pe_strike: Optional[int] = None
        self.pe_entry_price: Optional[float] = None
        self.last_roll_time: Optional[datetime.datetime] = None
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.ledger: set[str] = set()
        self.tsym_map: Dict[str, str] = {}
        self.terminated: bool = False
        self.our_order_ids: set[str] = set()
        self.reentry_attempts: int = 0
        self.last_reentry_time: Optional[datetime.datetime] = None
        self.last_ltp_map: Dict[str, float] = {}
        # Persist closed-leg details and realized P&L across the day
        self.closed_meta: Dict[str, Dict[str, Any]] = {}             # instrument -> {'entry_ltp': float|None, 'exit_ltp': float|None, 'closed_at': datetime}
        self.closed_rpnl_by_instrument: Dict[str, float] = {}        # instrument -> realized P&L captured at close
        self.closed_rpnl_by_tsym: Dict[str, float] = {}              # tsym_upper -> cumulative realized P&L for that instrument across all closes

    def log(self, msg: str): print(msg)
    def log_to_file(self, logfile_path, msg: str): write_logfile_entry(logfile_path, f"{msg}\n")

    def build_entry_message(self, ce_strike: int, pe_strike: int, lots: int = STRADDLE_LOTS) -> str:
        ce = build_option_symbol(SYMBOL, SELECTED_EXPIRY, ce_strike, "C")
        pe = build_option_symbol(SYMBOL, SELECTED_EXPIRY, pe_strike, "P")
        return f"{ce} sell {lots}, {pe} sell {lots}"

    def build_single_entry(self, instrument: str, lots: int = STRADDLE_LOTS) -> str:
        return f"{instrument} sell {lots}"

    def build_single_exit(self, instrument: str, lots: int = STRADDLE_LOTS) -> str:
        return f"{instrument} buy {lots}"

    def handle_margin_shortfall(self, reason_text: str, raw_resp: Optional[dict] = None):
        """
        Margin shortfall: exit all known positions and terminate this context for the day.
        """
        try:
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ❌ Margin shortfall detected: {reason_text}. Exiting all strategy positions; terminating context for the day.")
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] 🔍 Full broker response: {raw_resp}")
            self.terminated = True
            for instr in list(self.positions.keys()):
                try:
                    qty = self.positions.get(instr, {}).get("quantity", STRADDLE_LOTS)
                    (ok_exit, _, text_exit, raw_exit), tsym = broker_send_buy(self.webhook_url, instr, qty, label="margin-exit")
                    oid_exit = _extract_order_id_from_resp(raw_exit or {})
                    if oid_exit:
                        self.our_order_ids.add(oid_exit)
                    if ok_exit:
                        self._capture_closed_leg(instr, tsym, exit_ltp_hint=None)
                        self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ✅ Emergency exit placed for {instr} (tsym={tsym}).")
                        self.positions.pop(instr, None)
                        self.ledger.add(instr)
                    else:
                        self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Emergency exit FAILED for {instr}: {text_exit} | raw={raw_exit}")
                except Exception as e:
                    self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Exception while emergency exiting {instr}: {e}")
            self.ce_strike = None
            self.pe_strike = None
            self.ce_entry_price = None
            self.pe_entry_price = None
            self.in_position = False
        except Exception as e:
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Error in handle_margin_shortfall: {e}")

    def _bounded_sleep(self):
        time.sleep(ORDER_RETRY_BACKOFF_SECONDS)

    def _capture_closed_leg(self, instrument: str, tsym: Optional[str], exit_ltp_hint: Optional[float]):
        """
        Persist entry/exit LTP and realized P&L for a closed instrument.
        Also aggregate realized P&L by tsym so re-entries show a single consolidated row.
        """
        try:
            if instrument in self.closed_rpnl_by_instrument:
                # Already captured once; do not double-count
                return

            # Resolve exit_ltp best-effort
            exit_ltp = exit_ltp_hint
            tsu = str(tsym).upper() if tsym else None
            if (exit_ltp is None) and tsu:
                brow = _get_posrow_for_tsym(tsu, FLATTRADE_PRODUCT)
                exit_ltp = _get_ltp_from_posrow_or_quote(brow)

            pos = self.positions.get(instrument, {})
            entry_ltp = float(pos.get("entry_price") or 0.0) or None

            # Try broker rpnl first
            rpnl_val = None
            brow = None
            if tsu:
                brow = _get_posrow_for_tsym(tsu, FLATTRADE_PRODUCT)
                try:
                    if brow and "rpnl" in brow and brow.get("rpnl") is not None:
                        rpnl_val = float(brow.get("rpnl") or 0.0)
                except Exception:
                    rpnl_val = None

            # If broker doesn't provide rpnl, compute from entry/exit
            if rpnl_val is None:
                try:
                    lot_size = get_lot_size(SYMBOL)
                    qty_lots = int(pos.get("quantity") or STRADDLE_LOTS)
                    qty_units = qty_lots * lot_size
                    side = pos.get("side") or "S"
                    if entry_ltp is not None and exit_ltp is not None:
                        if side == "S":  # short
                            rpnl_val = (entry_ltp - float(exit_ltp)) * qty_units
                        else:  # long
                            rpnl_val = (float(exit_ltp) - entry_ltp) * qty_units
                except Exception:
                    rpnl_val = None

            # Store meta and rpnl
            self.closed_meta[instrument] = {
                "entry_ltp": entry_ltp,
                "exit_ltp": float(exit_ltp) if exit_ltp not in (None, 0, 0.0) else None,
                "closed_at": datetime.datetime.now(),
            }
            if rpnl_val is not None:
                self.closed_rpnl_by_instrument[instrument] = float(rpnl_val)
                if tsu:
                    self.closed_rpnl_by_tsym[tsu] = self.closed_rpnl_by_tsym.get(tsu, 0.0) + float(rpnl_val)
        except Exception:
            pass

    def exit_instrument_until_success(self, instrument: str, current_ltp_hint: Optional[float] = None, label: str = "exit"):
        tsym = self.tsym_map.get(instrument) or tsym_from_internal(instrument)
        broker_net_units = _broker_netqty_for_tsym_product(tsym, FLATTRADE_PRODUCT) if tsym else 0
        if broker_net_units == 0:
            self._capture_closed_leg(instrument, tsym, exit_ltp_hint=current_ltp_hint)
            self.positions.pop(instrument, None)
            self.ledger.add(instrument)
            if instrument_opt_type(instrument) == "C":
                self.ce_strike = None; self.ce_entry_price = None
            elif instrument_opt_type(instrument) == "P":
                self.pe_strike = None; self.pe_entry_price = None
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ℹ️ Broker net=0 for {tsym}; skipping exit and clearing local tracking.")
            return True

        lot_size = get_lot_size(SYMBOL)
        exit_lots = max(1, int(abs(broker_net_units) // lot_size))
        attempt = 1
        while attempt <= MAX_ORDER_RETRY_ATTEMPTS:
            exit_ltp = current_ltp_hint
            if exit_ltp is None and tsym:
                brow = _get_posrow_for_tsym(tsym, FLATTRADE_PRODUCT)
                exit_ltp = _get_ltp_from_posrow_or_quote(brow)
            (ok, status, text, raw), tsym_resp = broker_send_buy(self.webhook_url, instrument, exit_lots, f"{label} (attempt {attempt})")
            oid_exit = _extract_order_id_from_resp(raw or {})
            if oid_exit:
                self.our_order_ids.add(oid_exit)
            if ok:
                self._capture_closed_leg(instrument, tsym_resp, exit_ltp_hint=exit_ltp)
                self.positions.pop(instrument, None)
                if instrument_opt_type(instrument) == "C":
                    self.ce_strike = None; self.ce_entry_price = None
                elif instrument_opt_type(instrument) == "P":
                    self.pe_strike = None; self.pe_entry_price = None
                self.tsym_map[instrument] = tsym_resp
                self.ledger.add(instrument)
                return True
            if raw and _is_margin_error(raw):
                self.handle_margin_shortfall(_extract_broker_message(raw), raw)
                return False
            if text and _is_margin_error(text):
                self.handle_margin_shortfall(_extract_broker_message(text))
                return False
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ❌ {label} failed; retrying in {ORDER_RETRY_BACKOFF_SECONDS}s... ({text})")
            self._bounded_sleep(); attempt += 1

        self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Max exit attempts reached for {instrument}.")
        return False

    def enter_instrument_until_success(self, instrument: str, entry_price: Optional[float] = None, label: str = "entry"):
        # Prevent duplicate sell if broker already shows the short (external/manual entry)
        tsym = self.tsym_map.get(instrument) or tsym_from_internal(instrument)
        broker_net_units = _broker_netqty_for_tsym_product(tsym, FLATTRADE_PRODUCT) if tsym else 0
        lot_size = get_lot_size(SYMBOL)
        expected_units = -int(STRADDLE_LOTS) * lot_size
        if broker_net_units == expected_units:
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ℹ️ Broker already has expected short for {instrument}; skipping duplicate sell.")
            self.positions[instrument] = {"side": "S", "entry_price": entry_price if entry_price is not None else 0.0, "quantity": STRADDLE_LOTS, "mtm": 0.0}
            strike_extracted = extract_strike_from_symbol(instrument)
            opt_type = instrument_opt_type(instrument)
            if opt_type == "C":
                self.ce_strike = strike_extracted; self.ce_entry_price = entry_price
            elif opt_type == "P":
                self.pe_strike = strike_extracted; self.pe_entry_price = entry_price
            self.tsym_map[instrument] = tsym
            # Do not add to ledger at entry; ensure no stale ledger entry
            self.ledger.discard(instrument)
            return True

        if instrument in self.positions and self.positions[instrument].get("side") == "S":
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Skipping duplicate sell for {instrument} (already short locally).")
            return True
        lots = STRADDLE_LOTS
        attempt = 1
        while attempt <= MAX_ORDER_RETRY_ATTEMPTS:
            (ok, status, text, raw), tsym = broker_send_sell(self.webhook_url, instrument, lots, f"{label} (attempt {attempt})")
            oid_entry = _extract_order_id_from_resp(raw or {})
            if oid_entry:
                self.our_order_ids.add(oid_entry)
            if ok:
                self.positions[instrument] = {"side": "S", "entry_price": entry_price if entry_price is not None else 0.0, "quantity": lots, "mtm": 0.0}
                strike_extracted = extract_strike_from_symbol(instrument)
                opt_type = instrument_opt_type(instrument)
                if opt_type == "C":
                    self.ce_strike = strike_extracted; self.ce_entry_price = entry_price
                elif opt_type == "P":
                    self.pe_strike = strike_extracted; self.pe_entry_price = entry_price
                self.tsym_map[instrument] = tsym
                # Do not add to ledger at entry; ensure no stale ledger entry
                self.ledger.discard(instrument)
                return True
            if raw and _is_margin_error(raw):
                self.handle_margin_shortfall(_extract_broker_message(raw), raw)
                return False
            if text and _is_margin_error(text):
                self.handle_margin_shortfall(_extract_broker_message(text))
                return False
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ❌ {label} failed; retrying in {ORDER_RETRY_BACKOFF_SECONDS}s... ({text})")
            self._bounded_sleep(); attempt += 1

        self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Max entry attempts reached for {instrument}.")
        return False

    def enter_straddle_until_success(self, ce_strike: int, pe_strike: int, expiry_raw: str, ce_entry_price: Optional[float] = None, pe_entry_price: Optional[float] = None, lots: int = STRADDLE_LOTS) -> bool:
        ce_instr = build_option_symbol(SYMBOL, expiry_raw, ce_strike, "C")
        pe_instr = build_option_symbol(SYMBOL, expiry_raw, pe_strike, "P")
        attempt = 1
        while attempt <= MAX_ORDER_RETRY_ATTEMPTS:
            (ok_ce, _, text_ce, raw_ce), tsym_ce = broker_send_sell(self.webhook_url, ce_instr, lots, f"entry-straddle-CE (attempt {attempt})")
            (ok_pe, _, text_pe, raw_pe), tsym_pe = broker_send_sell(self.webhook_url, pe_instr, lots, f"entry-straddle-PE (attempt {attempt})")
            oid_ce = _extract_order_id_from_resp(raw_ce or {})
            oid_pe = _extract_order_id_from_resp(raw_pe or {})
            if oid_ce: self.our_order_ids.add(oid_ce)
            if oid_pe: self.our_order_ids.add(oid_pe)
            if ok_ce and ok_pe:
                self.positions[ce_instr] = {"side": "S", "entry_price": ce_entry_price if ce_entry_price is not None else 0.0, "quantity": lots, "mtm": 0.0}
                self.positions[pe_instr] = {"side": "S", "entry_price": pe_entry_price if pe_entry_price is not None else 0.0, "quantity": lots, "mtm": 0.0}
                self.ce_strike = ce_strike; self.ce_entry_price = ce_entry_price
                self.pe_strike = pe_strike; self.pe_entry_price = pe_entry_price
                self.tsym_map[ce_instr] = tsym_ce
                self.tsym_map[pe_instr] = tsym_pe
                # Do not add to ledger at entry
                self.ledger.discard(ce_instr); self.ledger.discard(pe_instr)
                return True

            combined_raw = {}
            if isinstance(raw_ce, dict):
                combined_raw.update(raw_ce)
            if isinstance(raw_pe, dict):
                combined_raw.update(raw_pe)
            combined_text = " ".join(filter(None, [str(text_ce or ""), str(text_pe or "")]))
            if (combined_raw and _is_margin_error(combined_raw)) or (combined_text and _is_margin_error(combined_text)):
                self.handle_margin_shortfall(_extract_broker_message(combined_raw) or _extract_broker_message(combined_text), combined_raw or None)
                return False

            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ❌ entry-straddle failed; retrying in {ORDER_RETRY_BACKOFF_SECONDS}s... (CE:{text_ce} | PE:{text_pe})")
            self._bounded_sleep(); attempt += 1

        # After max attempts: ensure neutrality by closing any partial fills at broker
        ts_ce = tsym_from_internal(ce_instr)
        ts_pe = tsym_from_internal(pe_instr)
        net_ce = _broker_netqty_for_tsym_product(ts_ce, FLATTRADE_PRODUCT)
        net_pe = _broker_netqty_for_tsym_product(ts_pe, FLATTRADE_PRODUCT)
        if net_ce != 0:
            self.exit_instrument_until_success(ce_instr, current_ltp_hint=None, label="abort-entry-exit-CE")
        if net_pe != 0:
            self.exit_instrument_until_success(pe_instr, current_ltp_hint=None, label="abort-entry-exit-PE")
        self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Max straddle entry attempts reached; stayed/returned to flat.")
        return False

# =====================
# ENTRY VERIFICATION (BROKER-TRUTH)
# =====================

def _instrument_to_tsym(ctx: "StrategyContext", instrument: str) -> str:
    # Prefer previously seen broker tsym for the same instrument; otherwise derive it
    return ctx.tsym_map.get(instrument) or tsym_from_internal(instrument)

def verify_entry_and_squareoff_if_needed(ctx: "StrategyContext", ce_internal: str, pe_internal: str, logfile_path: str, wait_seconds: float = 1.0) -> bool:
    """
    Verify broker PositionBook shows expected net units (negative for short) for CE and PE after an attempted entry.
    If either leg is not at exact expected short size, emergency-exit any filled legs, mark ctx.terminated and return False.
    Otherwise return True.
    """
    try:
        ts_ce = str(_instrument_to_tsym(ctx, ce_internal)).upper() if ce_internal else None
        ts_pe = str(_instrument_to_tsym(ctx, pe_internal)).upper() if pe_internal else None

        expected_units = -int(STRADDLE_LOTS) * int(get_lot_size(SYMBOL))

        deadline = time.time() + max(1.0, float(wait_seconds))
        while time.time() < deadline:
            net_ce = _broker_netqty_for_tsym_product(ts_ce, FLATTRADE_PRODUCT) if ts_ce else 0
            net_pe = _broker_netqty_for_tsym_product(ts_pe, FLATTRADE_PRODUCT) if ts_pe else 0
            if net_ce == expected_units and net_pe == expected_units:
                return True
            time.sleep(0.3)

        net_ce = _broker_netqty_for_tsym_product(ts_ce, FLATTRADE_PRODUCT) if ts_ce else 0
        net_pe = _broker_netqty_for_tsym_product(ts_pe, FLATTRADE_PRODUCT) if ts_pe else 0

        if net_ce != expected_units or net_pe != expected_units:
            msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Entry verification mismatch (broker): CE net={net_ce} (exp {expected_units}) | PE net={net_pe} (exp {expected_units}). Exiting any filled legs..."
            ctx.log(msg); ctx.log_to_file(logfile_path, msg)

            for instr in list(ctx.positions.keys()):
                try:
                    ctx.exit_instrument_until_success(instr, current_ltp_hint=None, label="entry-mismatch-exit")
                except Exception as e:
                    ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Emergency exit failed for {instr}: {e}")
                    ctx.log_to_file(logfile_path, f"[{nowstr()}] ⚠️ Emergency exit failed for {instr}: {e}")

            ctx.in_position = False
            ctx.ce_strike = ctx.pe_strike = None
            ctx.ce_entry_price = ctx.pe_entry_price = None
            ctx.terminated = True
            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after entry mismatch.")
            ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after entry mismatch.")
            return False

        return True

    except Exception as ex:
        ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Exception during entry verification: {ex}")
        ctx.log_to_file(logfile_path, f"[{nowstr()}] ⚠️ Exception during entry verification: {ex}")
        return False

# =====================
# NEUTRALITY + RECONCILIATION
# =====================

def enforce_neutrality_or_squareoff(ctx: "StrategyContext", logfile_path: str) -> bool:
    """
    Ensure both legs are active while strategy is in_position.
    If exactly one leg becomes flat (broker net=0), auto-close the other leg too.
    Returns True if it performed any exit, else False.
    """
    try:
        if not ctx.in_position:
            return False

        ce_internal = build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.ce_strike, "C") if ctx.ce_strike else None
        pe_internal = build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.pe_strike, "P") if ctx.pe_strike else None
        ts_ce = str(_instrument_to_tsym(ctx, ce_internal)).upper() if ce_internal else None
        ts_pe = str(_instrument_to_tsym(ctx, pe_internal)).upper() if pe_internal else None

        net_ce = _broker_netqty_for_tsym_product(ts_ce, FLATTRADE_PRODUCT) if ts_ce else 0
        net_pe = _broker_netqty_for_tsym_product(ts_pe, FLATTRADE_PRODUCT) if ts_pe else 0

        ce_flat = (net_ce == 0)
        pe_flat = (net_pe == 0)

        if ce_flat ^ pe_flat:
            remaining_instr = pe_internal if ce_flat else ce_internal
            which = "PE" if ce_flat else "CE"
            msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 Neutrality guard: {('CE' if ce_flat else 'PE')} leg is flat at broker; auto-closing remaining {which} leg."
            ctx.log(msg); ctx.log_to_file(logfile_path, msg)
            ctx.exit_instrument_until_success(remaining_instr, current_ltp_hint=None, label="neutrality-exit")

            ctx.in_position = False
            ctx.ce_strike = None; ctx.ce_entry_price = None
            ctx.pe_strike = None; ctx.pe_entry_price = None

            info = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Both legs flat after neutrality guard. Strategy will consider re-entry on next cycle."
            ctx.log(info); ctx.log_to_file(logfile_path, info)
            return True

        return False

    except Exception as e:
        ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Neutrality guard error: {e}")
        ctx.log_to_file(logfile_path, f"[{nowstr()}] ⚠️ Neutrality guard error: {e}")
        return False

def reconcile_open_positions(ctx: "StrategyContext", logfile_path: str) -> bool:
    """
    Continuous reconciliation against broker PositionBook:
    - Detect manual exits for tracked instruments (netqty==0) and clear local state (strict confirmation).
    - If exactly one leg remains open, auto-close it to maintain neutrality.
    """
    changed = False
    try:
        for instrument in list(ctx.positions.keys()):
            tsym = ctx.tsym_map.get(instrument) or tsym_from_internal(instrument)
            if _confirm_broker_flat(tsym, FLATTRADE_PRODUCT):
                ctx._capture_closed_leg(instrument, tsym, exit_ltp_hint=None)
                ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Manual/External exit confirmed for {instrument} (broker flat). Clearing local.")
                ctx.log_to_file(logfile_path, f"[{nowstr()}] ℹ️ Manual/External exit confirmed for {instrument} (broker flat).")
                ctx.positions.pop(instrument, None)
                ctx.ledger.add(instrument)
                opt_type = instrument_opt_type(instrument)
                if opt_type == "C":
                    ctx.ce_strike = None; ctx.ce_entry_price = None
                elif opt_type == "P":
                    ctx.pe_strike = None; ctx.pe_entry_price = None
                changed = True

        if ctx.in_position:
            ce_internal = build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.ce_strike, "C") if ctx.ce_strike else None
            pe_internal = build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.pe_strike, "P") if ctx.pe_strike else None
            ts_ce = str(_instrument_to_tsym(ctx, ce_internal)).upper() if ce_internal else None
            ts_pe = str(_instrument_to_tsym(ctx, pe_internal)).upper() if pe_internal else None
            net_ce = _broker_netqty_for_tsym_product(ts_ce, FLATTRADE_PRODUCT) if ts_ce else 0
            net_pe = _broker_netqty_for_tsym_product(ts_pe, FLATTRADE_PRODUCT) if ts_pe else 0
            ce_open = net_ce != 0
            pe_open = net_pe != 0
            if ce_open ^ pe_open:
                remaining_instr = ce_internal if ce_open else pe_internal
                which = "CE" if ce_open else "P"
                msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 Reconciliation: Only {which} leg open at broker; exiting to maintain neutrality."
                ctx.log(msg); ctx.log_to_file(logfile_path, msg)
                ctx.exit_instrument_until_success(remaining_instr, current_ltp_hint=None, label="reconcile-neutrality-exit")
                ctx.in_position = False
                ctx.ce_strike = None; ctx.ce_entry_price = None
                ctx.pe_strike = None; ctx.pe_entry_price = None
                changed = True
    except Exception as e:
        ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Reconciliation error: {e}")
        ctx.log_to_file(logfile_path, f"[{nowstr()}] ⚠️ Reconciliation error: {e}")
    return changed

# =====================
# MTM + ROWS (ISOLATED SCOPE, CONSOLIDATED PER TSYM)
# =====================

def broker_mtm_for_ctx(ctx: "StrategyContext", chain_data: Optional[List[dict]] = None) -> Tuple[float, float, float, List[Dict[str, Any]]]:
    """
    Returns (total_mtm, sum_rpnl, sum_ur, rows[]) scoped to this context.
    ISOLATE mode (consolidated):
      - For each instrument tsym (open or previously seen/closed), display a SINGLE consolidated row:
          netqty from broker (if open, else 0),
          rpnl = persisted (closed_rpnl_by_tsym) + broker rpnl if broker returns closed row,
          urmtom = open UR from entry vs current LTP (if open; else 0),
          entry/current LTP fields filled appropriately.
      - No duplicate rows for the same tsym (even if both open and closed history exists).
    """
    rows: List[Dict[str, Any]] = []

    if ISOLATE_BY_OUR_ORDERS:
        sum_rpnl = 0.0
        sum_ur = 0.0
        lot_size = get_lot_size(SYMBOL)

        instruments_positions = list(ctx.positions.keys())
        instruments_ledger = list(ctx.ledger)

        # Build tsym lists
        pos_tsyms = [str(_instrument_to_tsym(ctx, ins)).upper() for ins in instruments_positions]
        led_tsyms = [str(_instrument_to_tsym(ctx, ins)).upper() for ins in instruments_ledger]
        all_tsyms_unique = list(set(pos_tsyms + led_tsyms + list(ctx.closed_rpnl_by_tsym.keys())))

        # Pull broker rows only for our scope
        pb_map = _pb_rows_for_tsyms(all_tsyms_unique, FLATTRADE_PRODUCT)

        consolidated: Dict[str, Dict[str, Any]] = {}

        # Consolidate OPEN legs first
        for instrument, pos in list(ctx.positions.items()):
            try:
                tsym = str(_instrument_to_tsym(ctx, instrument)).upper()
                strike = extract_strike_from_symbol(instrument)
                opt_letter = instrument_opt_type(instrument) or ("C" if "C" in tsym else "P")
                brow = pb_map.get(tsym) or _get_posrow_for_tsym(tsym, FLATTRADE_PRODUCT)
                ltp = _get_ltp_from_posrow_or_quote(brow)
                if (ltp is None) or (float(ltp) <= 0.0):
                    ltp = get_leg_ltp_from_chain(chain_data or [], strike, opt_letter) if strike is not None else None
                if (ltp is None) or (float(ltp) <= 0.0):
                    ltp = ctx.last_ltp_map.get(tsym)
                entry_price = float(pos.get("entry_price") or 0.0)
                if not entry_price or entry_price == 0.0:
                    try:
                        if brow and brow.get("netavgprc"):
                            entry_price = float(brow.get("netavgprc"))
                    except Exception:
                        pass
                    if ((not entry_price or entry_price == 0.0) and ltp is not None):
                        entry_price = float(ltp)
                if (ltp is None) or (float(ltp) <= 0.0):
                    ltp = entry_price
                try:
                    ctx.last_ltp_map[tsym] = float(ltp)
                except Exception:
                    pass

                qty_lots = int(pos.get("quantity") or STRADDLE_LOTS)
                qty_units = qty_lots * lot_size
                netqty_broker = int(float((brow or {}).get("netqty", 0) or 0))
                ur = 0.0 if netqty_broker == 0 else ((entry_price - float(ltp)) * qty_units if pos.get("side") == "S" else (float(ltp) - entry_price) * qty_units)

                base_rpnl = ctx.closed_rpnl_by_tsym.get(tsym, 0.0)  # persisted realized P&L for this tsym
                consolidated[tsym] = {
                    "tsym": tsym,
                    "netqty": netqty_broker,
                    "rpnl": float(base_rpnl),
                    "urmtom": float(ur),
                    "entry_ltp": entry_price,
                    "current_ltp": float(ltp),
                }
            except Exception:
                continue

        # Consolidate CLOSED legs (only for tsym not currently open or to accumulate broker rpnl)
        for tsym in all_tsyms_unique:
            try:
                tsu = str(tsym).upper()
                if tsu in consolidated:
                    # If broker also returns a closed rpnl row for the same tsym (netqty==0), add it to rpnl accumulator
                    brow = pb_map.get(tsu)
                    if brow:
                        try:
                            netqty = int(float(brow.get("netqty", 0) or 0))
                        except Exception:
                            netqty = 0
                        if netqty == 0:
                            try:
                                consolidated[tsu]["rpnl"] += float(brow.get("rpnl", 0.0) or 0.0)
                            except Exception:
                                pass
                    continue

                # tsym not open currently; still show a single consolidated row with persisted rpnl and zero UR
                brow = pb_map.get(tsu)
                rpnl_val = ctx.closed_rpnl_by_tsym.get(tsu, 0.0)
                if brow:
                    try:
                        netqty = int(float(brow.get("netqty", 0) or 0))
                    except Exception:
                        netqty = 0
                    if netqty == 0:
                        try:
                            rpnl_val += float(brow.get("rpnl", 0.0) or 0.0)
                        except Exception:
                            pass
                consolidated[tsu] = {
                    "tsym": tsu,
                    "netqty": 0,
                    "rpnl": float(rpnl_val),
                    "urmtom": 0.0,
                    "entry_ltp": (ctx.closed_meta.get(tsu, {}) or {}).get("entry_ltp"),   # may be None
                    "current_ltp": (ctx.closed_meta.get(tsu, {}) or {}).get("exit_ltp"), # may be None
                }
            except Exception:
                continue

        # Emit consolidated rows (single row per tsym)
        for tsu, row in consolidated.items():
            try:
                rows.append(row)
                sum_rpnl += float(row.get("rpnl", 0.0) or 0.0)
                sum_ur += float(row.get("urmtom", 0.0) or 0.0)
            except Exception:
                continue

        total_mtm = sum_rpnl + sum_ur
        return total_mtm, sum_rpnl, sum_ur, rows

    # Default: PositionBook mode (account-wide)
    pb = flattrade_fetch_positionbook()
    rows: List[Dict[str, Any]] = []
    if not pb:
        return 0.0, 0.0, 0.0, rows

    wanted_tsyms = set()
    for internal in (set(ctx.positions.keys()) | ctx.ledger):
        ts = ctx.tsym_map.get(internal)
        if not ts:
            try:
                ts = tsym_from_internal(internal)
            except Exception:
                ts = None
        if ts:
            wanted_tsyms.add(str(ts).upper())

    sum_rpnl = 0.0
    sum_ur = 0.0
    for row in pb:
        try:
            ts = str(row.get("tsym", "")).upper()
            if ts not in wanted_tsyms:
                continue
            rpnl = float(row.get("rpnl", 0.0))
            netqty = int(float(row.get("netqty", 0)))
            ur = float(row.get("urmtom", 0.0)) if netqty != 0 else 0.0
            rows.append({"tsym": ts, "netqty": netqty, "rpnl": rpnl, "urmtom": ur, "entry_ltp": None, "current_ltp": None})
            sum_rpnl += rpnl
            sum_ur += ur
        except Exception:
            continue
    return sum_rpnl + sum_ur, sum_rpnl, sum_ur, rows

def compute_strategy_mtms(ctx: "StrategyContext") -> Tuple[float, float]:
    """
    ISOLATE mode:
      CE_total/PE_total = sum(UR from open CE/PE legs) + sum(persisted RPNL from closed CE/PE legs).
      Broker closed rows are not required for Strategy MTM; we rely on persisted store to avoid drop-offs.
    """
    if ISOLATE_BY_OUR_ORDERS:
        try:
            data = get_option_chain_from_upstox(SELECTED_EXPIRY)
        except Exception:
            data = []
        ce_total = 0.0
        pe_total = 0.0
        lot_size = get_lot_size(SYMBOL)

        # Open legs: UR from entry vs current LTP, grouped by opt type
        for instrument, pos in ctx.positions.items():
            try:
                strike = extract_strike_from_symbol(instrument)
                opt_letter = instrument_opt_type(instrument) or ("C" if "C" in instrument else "P")
                tsym = str(_instrument_to_tsym(ctx, instrument)).upper()

                brow = _get_posrow_for_tsym(tsym, FLATTRADE_PRODUCT)
                ltp = _get_ltp_from_posrow_or_quote(brow)
                if (ltp is None) or (float(ltp) <= 0.0):
                    ltp = get_leg_ltp_from_chain(data, strike, opt_letter) if strike is not None else None
                if (ltp is None) or (float(ltp) <= 0.0):
                    ltp = ctx.last_ltp_map.get(tsym)

                entry = float(pos.get("entry_price") or 0.0)
                if not entry or entry == 0.0:
                    try:
                        if brow and brow.get("netavgprc"):
                            entry = float(brow.get("netavgprc"))
                    except Exception:
                        pass
                    if ((not entry or entry == 0.0) and ltp is not None):
                        entry = float(ltp)
                if (ltp is None) or (float(ltp) <= 0.0):
                    ltp = entry

                qty_units = int(pos.get("quantity") or STRADDLE_LOTS) * lot_size
                pnl = (entry - float(ltp)) * qty_units if pos.get("side") == "S" else (float(ltp) - entry) * qty_units
                if opt_letter == "C":
                    ce_total += pnl
                else:
                    pe_total += pnl
            except Exception:
                continue

        # Closed legs: sum persisted realized P&L by type (no duplicates)
        if CAPTURE_CLOSED_RPNL_IN_STRATEGY and CAPTURE_CLOSED_ENTRIES:
            for instrument, rpnl in list(ctx.closed_rpnl_by_instrument.items()):
                try:
                    opt = instrument_opt_type(instrument)
                    if opt == "C":
                        ce_total += float(rpnl)
                    elif opt == "P":
                        pe_total += float(rpnl)
                except Exception:
                    continue

        return ce_total, pe_total

    # Default: PositionBook mode (account-wide) — unchanged
    global BASELINE_PB_MAP
    pb = flattrade_fetch_positionbook()
    if not pb:
        return 0.0, 0.0

    wanted_tsyms = set()
    internals = set(ctx.positions.keys()) | set(ctx.ledger)
    try:
        if ctx.ce_strike:
            internals.add(build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.ce_strike, "C"))
        if ctx.pe_strike:
            internals.add(build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.pe_strike, "P"))
    except Exception:
        pass

    for internal in internals:
        try:
            ts = ctx.tsym_map.get(internal)
            if not ts:
                ts = tsym_from_internal(internal)
            if ts:
                wanted_tsyms.add(str(ts).upper())
        except Exception:
            continue

    ce_total = 0.0
    pe_total = 0.0

    for row in pb:
        try:
            ts = str(row.get("tsym", "")).upper()
            if ts not in wanted_tsyms:
                continue
            rpnl = float(row.get("rpnl", 0.0))
            netqty = int(float(row.get("netqty", 0)))
            ur = float(row.get("urmtom", 0.0)) if netqty != 0 else 0.0
            current_val = rpnl + ur
            baseline_val = 0.0
            if IGNORE_PAST_STRATEGY_POSITIONS:
                baseline_val = BASELINE_PB_MAP.get(ts, 0.0)
            val = current_val - baseline_val
            opt = _detect_option_type_from_tsym(ts)
            if opt == "C":
                ce_total += val
            elif opt == "P":
                pe_total += val
            else:
                if "CE" in ts:
                    ce_total += val
                elif "PE" in ts:
                    pe_total += val
                else:
                    continue
        except Exception:
            continue

    return ce_total, pe_total

def compute_account_total_mtm() -> float:
    pb = flattrade_fetch_positionbook()
    if not pb:
        return 0.0
    total = 0.0
    for row in pb:
        try:
            rpnl = float(row.get("rpnl", 0.0))
            netqty = int(float(row.get("netqty", 0)))
            ur = float(row.get("urmtom", 0.0)) if netqty != 0 else 0.0
            total += (rpnl + ur)
        except Exception:
            continue
    return total

# =====================
# MAIN LOOP + EOD FORCED EXIT + PCT WATCH WINDOWS
# =====================

def _can_attempt_reentry(ctx: StrategyContext, now: datetime.datetime) -> bool:
    if ctx.terminated:
        return False
    if ctx.reentry_attempts >= MAX_REENTRY_ATTEMPTS:
        ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⛔ Re-entry limit reached ({MAX_REENTRY_ATTEMPTS}). Staying flat for the day.")
        ctx.terminated = True
        return False
    if ctx.last_reentry_time:
        elapsed = (now - ctx.last_reentry_time).total_seconds()
        if elapsed < REENTRY_COOLDOWN_SECONDS:
            remaining = int(REENTRY_COOLDOWN_SECONDS - elapsed)
            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⏳ Re-entry cooldown active ({remaining}s remaining).")
            return False
    return True

def _record_reentry(ctx: StrategyContext, now: datetime.datetime, success: bool):
    if success:
        ctx.reentry_attempts = 0
        ctx.last_reentry_time = now
    else:
        ctx.reentry_attempts += 1
        ctx.last_reentry_time = now
        if ctx.reentry_attempts >= MAX_REENTRY_ATTEMPTS:
            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⛔ Max re-entry attempts reached. Terminating for the day.")
            ctx.terminated = True

def _exit_all_broker_open_legs(ctx: StrategyContext, label: str = "forced-exit"):
    """
    Exit both CE and PE instruments if broker shows non-zero netqty, regardless of local tracking.
    Prevents one-leg-left-open at EOD.
    """
    try:
        ce_internal = build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.ce_strike, "C") if ctx.ce_strike else None
        pe_internal = build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.pe_strike, "P") if ctx.pe_strike else None
        for instr in [ce_internal, pe_internal]:
            if not instr:
                continue
            tsym = tsym_from_internal(instr)
            net = _broker_netqty_for_tsym_product(tsym, FLATTRADE_PRODUCT)
            if net != 0:
                ctx.exit_instrument_until_success(instr, current_ltp_hint=None, label=label)
    except Exception as e:
        ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ EOD forced exit error: {e}")

def main_loop(strategies_config: List[StrategyContext]):
    # Dynamic expiry selection (auto-pick). Fallback to static EXPIRY_DATE if probe fails.
    global SELECTED_EXPIRY
    try:
        _probed = find_nearest_expiry_by_probe() or EXPIRY_DATE
    except Exception as _e:
        print(f"[{nowstr()}] ⚠️ Expiry probe error: {_e}. Falling back to {EXPIRY_DATE}")
        _probed = EXPIRY_DATE
    SELECTED_EXPIRY = _probed
    NEAREST_EXPIRY = _probed

    logfile_path = get_logfile_path()
    print(f"[{nowstr()}] 🔹 Using expiry: {NEAREST_EXPIRY}")
    print(f"[{nowstr()}] 🚀 Starting multi-pct {SYMBOL} straddle-rolling. Active PCTs: {', '.join(str(ctx.trigger_pct)+'%' for ctx in strategies_config)}")
    print(f"[{nowstr()}] 🔹 Tick log also saved to: {logfile_path}")
    print(f"[{nowstr()}] 🔧 Broker: FLATTRADE | UID={(FT_UID or '??')} | PRD={FLATTRADE_PRODUCT} | EXCH={FLATTRADE_EXCHANGE}")

    # Basic credential checks
    if not (FT_UID and FT_ACTID and FT_JKEY):
        print(f"[{nowstr()}] ❌ Missing Flattrade creds (FT_UID/FT_ACTID/FT_JKEY) in script. Edit the file and retry.")
        return

    # Capture baseline once if configured to IGNORE past strategy positions
    global BASELINE_PB_MAP
    if IGNORE_PAST_STRATEGY_POSITIONS:
        try:
            pb_init = flattrade_fetch_positionbook()
            BASELINE_PB_MAP = {}
            if pb_init:
                for row in pb_init:
                    try:
                        ts = str(row.get("tsym", "")).upper()
                        if not ts.startswith(SYMBOL.upper()):
                            continue
                        rpnl = float(row.get("rpnl", 0.0))
                        netqty = int(float(row.get("netqty", 0)))
                        ur = float(row.get("urmtom", 0.0)) if netqty != 0 else 0.0
                        BASELINE_PB_MAP[ts] = rpnl + ur
                    except Exception:
                        continue
                print(f"[{nowstr()}] 🔎 Baseline PositionBook captured for fresh MTM (ignored past positions): {len(BASELINE_PB_MAP)} rows")
            else:
                BASELINE_PB_MAP = {}
        except Exception as e:
            print(f"[{nowstr()}] ⚠️ Could not capture baseline PositionBook: {e}")
            BASELINE_PB_MAP = {}

    try:
        while True:
            now = datetime.datetime.now()

            # Start-time gating:
            if PCT_START_TIME is None:
                # Original behavior: block until START_TIME
                if now.time() < START_TIME:
                    print(f"[{nowstr()}] ⏳ Waiting for market open at {START_TIME}")
                    write_logfile_entry(logfile_path, f"[{nowstr()}] ⏳ Waiting for market open at {START_TIME}\n")
                    time.sleep(10)
                    continue
            else:
                # PCT watch mode: block until PCT_START_TIME (watch begins). After this, allow early entry based on diff% even before START_TIME.
                if now.time() < PCT_START_TIME:
                    print(f"[{nowstr()}] ⏳ Waiting for PCT start watch at {PCT_START_TIME}")
                    write_logfile_entry(logfile_path, f"[{nowstr()}] ⏳ Waiting for PCT start watch at {PCT_START_TIME}\n")
                    time.sleep(10)
                    continue

            # Exit-time hard stop (only if PCT end does not trigger earlier) — handled later as well
            if now.time() >= EXIT_TIME:
                for ctx in strategies_config:
                    if ctx.in_position:
                        ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 Exit time reached. Closing positions...")
                        ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 Exit time reached. Closing positions...")
                        reconcile_open_positions(ctx, logfile_path)
                        _exit_all_broker_open_legs(ctx, label="exit-eod")
                        ctx.in_position = False
                        ctx.ce_strike = ctx.pe_strike = None
                        ctx.ce_entry_price = ctx.pe_entry_price = None
                print(f"[{nowstr()}] ✅ Market closed. Exiting multi-strategy loop.")
                write_logfile_entry(logfile_path, f"[{nowstr()}] ✅ Market closed. Exiting multi-strategy loop.\n")
                break

            data = get_option_chain_from_upstox(NEAREST_EXPIRY)
            if not data:
                msg = f"[{nowstr()}] ⚠️ No option chain received, retrying in {REFRESH_INTERVAL}s"
                print(msg); write_logfile_entry(logfile_path, msg + "\n")
                time.sleep(REFRESH_INTERVAL)
                continue

            # Find ATM and CE/PE diff% for watch logic
            spot, atm_strike, _ = pick_atm_from_chain(data)
            atm_diff_pct = compute_atm_cepe_diff_pct(data, atm_strike)

            # PCT END watch: if configured and after PCT_END_TIME but before EXIT_TIME, exit immediately when diff% <= threshold
            if (PCT_END_TIME is not None) and (now.time() >= PCT_END_TIME) and (now.time() < EXIT_TIME):
                if atm_diff_pct is not None and atm_diff_pct <= float(PCT_END_DIFF):
                    print(f"[{nowstr()}] 🛑 PCT end watch: ATM CE/PE diff%={atm_diff_pct:.2f} <= {PCT_END_DIFF:.2f}% — Exiting strategies for the day.")
                    write_logfile_entry(logfile_path, f"[{nowstr()}] 🛑 PCT end watch: diff%={atm_diff_pct:.2f} <= {PCT_END_DIFF:.2f}% — Exit.\n")
                    for ctx in strategies_config:
                        try:
                            if ctx.in_position:
                                ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 PCT end-trigger exit. Closing positions...")
                                ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 PCT end-trigger exit. Closing positions...")
                                reconcile_open_positions(ctx, logfile_path)
                                _exit_all_broker_open_legs(ctx, label="exit-pct-end")
                                ctx.in_position = False
                                ctx.ce_strike = ctx.pe_strike = None
                                ctx.ce_entry_price = ctx.pe_entry_price = None
                                ctx.terminated = True
                        except Exception as e:
                            print(f"[{nowstr()}] ⚠️ Error during PCT end-trigger exit: {e}")
                    print(f"[{nowstr()}] ✅ PCT end-trigger exit complete. Ending loop for the day.")
                    write_logfile_entry(logfile_path, f"[{nowstr()}] ✅ PCT end-trigger exit complete. Ending loop.\n")
                    break  # end of day

            for ctx in strategies_config:
                try:
                    # Continuous reconciliation with broker (manual exits / neutrality) — strict confirmation
                    reconcile_open_positions(ctx, logfile_path)

                    if atm_strike is None:
                        msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ No ATM determined from chain, skipping this cycle."
                        ctx.log(msg); ctx.log_to_file(logfile_path, msg + "\n\n")
                        print(); write_logfile_entry(logfile_path, "\n\n")
                        continue

                    # Entry gating: allow early entry before START_TIME only if PCT start is configured and diff% >= threshold
                    allow_entry_this_cycle = True
                    if (PCT_START_TIME is not None) and (now.time() < START_TIME):
                        # watch mode active
                        if atm_diff_pct is None or atm_diff_pct < float(PCT_START_DIFF):
                            allow_entry_this_cycle = False
                            msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 👀 PCT start-watch: diff%={atm_diff_pct if atm_diff_pct is not None else 'NA'} < {PCT_START_DIFF:.2f}% — waiting."
                            ctx.log(msg); ctx.log_to_file(logfile_path, msg)

                    # Initial entry or re-entry (respect re-entry gating)
                    if allow_entry_this_cycle and not ctx.in_position and not ctx.terminated and _can_attempt_reentry(ctx, now):
                        msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🚀 Entering straddle..."
                        ctx.log(msg); ctx.log_to_file(logfile_path, msg)
                        ce0 = get_leg_ltp_from_chain(data, atm_strike, "C")
                        pe0 = get_leg_ltp_from_chain(data, atm_strike, "P")
                        ok_entry = ctx.enter_straddle_until_success(atm_strike, atm_strike, NEAREST_EXPIRY, ce_entry_price=ce0, pe_entry_price=pe0, lots=STRADDLE_LOTS)
                        if not ok_entry:
                            _record_reentry(ctx, now, success=False)
                            continue
                        ctx.last_roll_time = now
                        ctx.in_position = True
                        ce_instr = build_option_symbol(SYMBOL, SELECTED_EXPIRY, atm_strike, "C")
                        pe_instr = build_option_symbol(SYMBOL, SELECTED_EXPIRY, atm_strike, "P")
                        ok = verify_entry_and_squareoff_if_needed(ctx, ce_instr, pe_instr, logfile_path, wait_seconds=1.0)
                        if not ok:
                            _record_reentry(ctx, now, success=False)
                            continue
                        _record_reentry(ctx, now, success=True)
                        write_logfile_entry(logfile_path, "\n\n")
                        continue

                    # Neutrality guard — if one leg flat, auto-close the other leg
                    if ctx.in_position and enforce_neutrality_or_squareoff(ctx, logfile_path):
                        continue

                    # Current LTPs for held legs (context)
                    ce_ltp = get_leg_ltp_from_chain(data, ctx.ce_strike, "C") if ctx.ce_strike else 0.0
                    pe_ltp = get_leg_ltp_from_chain(data, ctx.pe_strike, "P") if ctx.pe_strike else 0.0

                    # Broker-truth MTM details (running + closed rows if enabled) consolidated
                    total_mtm, sum_rpnl, sum_ur, rows = broker_mtm_for_ctx(ctx, chain_data=data)

                    # STRATEGY MTM (open UR + persisted closed RPNL)
                    strat_ce_total, strat_pe_total = compute_strategy_mtms(ctx)
                    strat_total = strat_ce_total + strat_pe_total

                    # ACCOUNT TOTAL MTM (all positions in account)
                    account_total = compute_account_total_mtm()

                    # Print Strategy MTM and TOTAL MTM lines
                    strat_line = f"Strategy MTM = CE {strat_ce_total:.2f} + PE {strat_pe_total:.2f} = {strat_total:.2f}"
                    print(strat_line); write_logfile_entry(logfile_path, strat_line + "\n")
                    total_line = f"TOTAL MTM = {account_total:.2f}"
                    print(total_line); write_logfile_entry(logfile_path, total_line + "\n")

                    # Logging based on style
                    if LOG_STYLE == "single":
                        ce_change = ((ce_ltp - (ctx.ce_entry_price or 0.0)) / (ctx.ce_entry_price or 1.0) * 100.0) if (ctx.ce_entry_price or 0.0) else 0.0
                        pe_change = ((pe_ltp - (ctx.pe_entry_price or 0.0)) / (ctx.pe_entry_price or 1.0) * 100.0) if (ctx.pe_entry_price or 0.0) else 0.0
                        summary = (f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] "
                                   f"Spot={spot:.2f} | ATM={atm_strike} | "
                                   f"CE {ctx.ce_strike}:{ce_ltp:.2f} ({ce_change:+.2f}%) | "
                                   f"PE {ctx.pe_strike}:{pe_ltp:.2f} ({pe_change:+.2f}%) | "
                                   f"MTM={total_mtm:.2f} (rpnl={sum_rpnl:.2f}, urmtom={sum_ur:.2f})")
                        ctx.log(summary); ctx.log_to_file(logfile_path, summary)
                        if SHOW_MTM_ROWS:
                            for r in rows:
                                line = log_mtm_row_aligned(r["tsym"], r["netqty"], r["rpnl"], r["urmtom"], r.get("entry_ltp"), r.get("current_ltp"))
                                ctx.log(line); ctx.log_to_file(logfile_path, line)
                        print(); print()
                        write_logfile_entry(logfile_path, "\n\n")
                    else:
                        l1 = log_atm_line(ctx.trigger_pct, spot, atm_strike, ctx.ce_strike, ce_ltp, ctx.pe_strike, pe_ltp, NEAREST_EXPIRY)
                        ctx.log(l1); ctx.log_to_file(logfile_path, l1)
                        if SHOW_MTM_ROWS and rows:
                            for r in rows:
                                l2 = log_mtm_row_aligned(r["tsym"], r["netqty"], r["rpnl"], r["urmtom"], r.get("entry_ltp"), r.get("current_ltp"))
                                ctx.log(l2); ctx.log_to_file(logfile_path, l2)
                        else:
                            l2 = log_mtm_total(ctx.trigger_pct, total_mtm, sum_rpnl, sum_ur)
                            ctx.log(l2); ctx.log_to_file(logfile_path, l2)
                        ce_change = ((ce_ltp - (ctx.ce_entry_price or 0.0)) / (ctx.ce_entry_price or 1.0) * 100.0) if (ctx.ce_entry_price or 0.0) else 0.0
                        pe_change = ((pe_ltp - (ctx.pe_entry_price or 0.0)) / (ctx.pe_entry_price or 1.0) * 100.0) if (ctx.pe_entry_price or 0.0) else 0.0
                        l3 = log_change_line_aligned(ce_change, pe_change)
                        ctx.log(l3); ctx.log_to_file(logfile_path, l3)
                        print(); print()
                        write_logfile_entry(logfile_path, "\n\n")

                    # Decide which metric to apply stop/target to:
                    if STOP_TARGET_ON_TOTAL:
                        check_val = account_total
                        metric_name = "TOTAL MTM"
                    else:
                        check_val = strat_total
                        metric_name = "Strategy MTM"

                    # Stops/targets based on configured metric
                    if ctx.in_position and ctx.positions:
                        if check_val <= -STOPLOSS_PER_LOT * STRADDLE_LOTS:
                            msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ {metric_name} stoploss hit! Exiting all positions..."
                            ctx.log(msg); ctx.log_to_file(logfile_path, msg)
                            for instr in list(ctx.positions.keys()):
                                ctx.exit_instrument_until_success(instr, current_ltp_hint=None, label="stoploss exit")
                            _exit_all_broker_open_legs(ctx, label="stoploss-exit")
                            ctx.in_position = False
                            ctx.ce_strike = ctx.pe_strike = None
                            ctx.ce_entry_price = ctx.pe_entry_price = None
                            ctx.terminated = True
                            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after stoploss on {metric_name}.")
                            ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after stoploss on {metric_name}.")
                            continue
                        elif check_val >= TARGET_PER_LOT * STRADDLE_LOTS:
                            msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🎯 {metric_name} target hit! Exiting all positions..."
                            ctx.log(msg); ctx.log_to_file(logfile_path, msg)
                            for instr in list(ctx.positions.keys()):
                                ctx.exit_instrument_until_success(instr, current_ltp_hint=None, label="target exit")
                            _exit_all_broker_open_legs(ctx, label="target-exit")
                            ctx.in_position = False
                            ctx.ce_strike = ctx.pe_strike = None
                            ctx.ce_entry_price = ctx.pe_entry_price = None
                            ctx.terminated = True
                            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after target on {metric_name}.")
                            ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after target on {metric_name}.")
                            continue

                    # Trigger/hold/ATM-change logic
                    ce_change_pct = ((ce_ltp - (ctx.ce_entry_price or 0.0)) / (ctx.ce_entry_price or 1.0) * 100.0) if (ctx.ce_entry_price or 0.0) else 0.0
                    pe_change_pct = ((pe_ltp - (ctx.pe_entry_price or 0.0)) / (ctx.pe_entry_price or 1.0) * 100.0) if (ctx.pe_entry_price or 0.0) else 0.0
                    triggered_ce = abs(ce_change_pct) >= ctx.trigger_pct
                    triggered_pe = abs(pe_change_pct) >= ctx.trigger_pct
                    hold_ok = (not HOLD_ENABLED) or ((now - ctx.last_roll_time) >= HOLD_TIME if ctx.last_roll_time else True)
                    ce_atm_changed = atm_strike != ctx.ce_strike if REQUIRE_ATM_CHANGE else True
                    pe_atm_changed = atm_strike != ctx.pe_strike if REQUIRE_ATM_CHANGE else True
                    ce_should_roll = triggered_ce and hold_ok and ce_atm_changed
                    pe_should_roll = triggered_pe and hold_ok and pe_atm_changed
                    if ce_should_roll != pe_should_roll:
                        time.sleep(0.75)
                    rolled_any = False

                    if ce_should_roll:
                        ce_exit_instr = build_option_symbol(SYMBOL, NEAREST_EXPIRY, ctx.ce_strike, "C")
                        ce_entry_instr = build_option_symbol(SYMBOL, NEAREST_EXPIRY, atm_strike, "C")
                        new_ce_ltp = get_leg_ltp_from_chain(data, atm_strike, "C")
                        if new_ce_ltp <= 0.0:
                            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Skipping CE roll: new CE LTP invalid. Baseline reset to current CE LTP.")
                            ctx.ce_entry_price = ce_ltp
                        else:
                            if ce_exit_instr in ctx.positions:
                                ctx.exit_instrument_until_success(ce_exit_instr, current_ltp_hint=ce_ltp, label="exit-partial-CE")
                            ctx.enter_instrument_until_success(ce_entry_instr, entry_price=new_ce_ltp, label="entry-partial-CE")
                            ctx.ce_strike = atm_strike
                            ctx.ce_entry_price = new_ce_ltp
                            ctx.last_roll_time = now
                            rolled_any = True
                            msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ✅ CE partial roll completed."
                            ctx.log(msg); ctx.log_to_file(logfile_path, msg)

                    if pe_should_roll:
                        pe_exit_instr = build_option_symbol(SYMBOL, NEAREST_EXPIRY, ctx.pe_strike, "P")
                        pe_entry_instr = build_option_symbol(SYMBOL, NEAREST_EXPIRY, atm_strike, "P")
                        new_pe_ltp = get_leg_ltp_from_chain(data, atm_strike, "P")
                        if new_pe_ltp <= 0.0:
                            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Skipping PE roll: new PE LTP invalid. Baseline reset to current PE LTP.")
                            ctx.pe_entry_price = pe_ltp
                        else:
                            if pe_exit_instr in ctx.positions:
                                ctx.exit_instrument_until_success(pe_exit_instr, current_ltp_hint=pe_ltp, label="exit-partial-PE")
                            ctx.enter_instrument_until_success(pe_entry_instr, entry_price=new_pe_ltp, label="entry-partial-PE")
                            ctx.pe_strike = atm_strike
                            ctx.pe_entry_price = new_pe_ltp
                            ctx.last_roll_time = now
                            rolled_any = True
                            msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ✅ PE partial roll completed."
                            ctx.log(msg); ctx.log_to_file(logfile_path, msg)

                    if not rolled_any:
                        if triggered_ce and hold_ok and not ce_atm_changed:
                            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🔄 CE triggered, ATM unchanged — baseline reset.")
                            ctx.ce_entry_price = ce_ltp
                        if triggered_pe and hold_ok and not pe_atm_changed:
                            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🔄 PE triggered, ATM unchanged — baseline reset.")
                            ctx.pe_entry_price = pe_ltp

                except Exception as e:
                    msg = f"[{nowstr()}] [PCT={ctx.trigger_pct}%] ⚠️ Strategy error: {e}"
                    print(msg); write_logfile_entry(logfile_path, msg + "\n")
                    write_logfile_entry(logfile_path, "\n")
                    continue

            time.sleep(REFRESH_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{nowstr()}] ⚠️ KeyboardInterrupt detected — attempting graceful shutdown for all strategies...")
        write_logfile_entry(logfile_path, f"[{nowstr()}] ⚠    KeyboardInterrupt detected — attempting graceful shutdown for all strategies...\n")
        for ctx in strategies_config:
            try:
                reconcile_open_positions(ctx, logfile_path)
                _exit_all_broker_open_legs(ctx, label="Manual Exit")
            except Exception as e:
                msg = f"⚠️ Could not auto-exit positions: {e}"
                ctx.log(msg); ctx.log_to_file(logfile_path, msg)
        print(f"[{nowstr()}] ✅ Graceful shutdown complete.")
        write_logfile_entry(logfile_path, f"[{nowstr()}] ✅ Graceful shutdown complete.\n")

if __name__ == "__main__":
    active_contexts = [StrategyContext(pct, url) for pct, url in STRATEGIES]
    if not active_contexts:
        print("No strategies enabled in STRATEGIES — uncomment entries to enable tests.")
    else:
        main_loop(active_contexts)
