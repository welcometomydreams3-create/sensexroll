#!/usr/bin/env python3
# multi_pct_sensex_straddle_roll_monitor_fixed_atm.py (Flattrade + Broker MTM, all config inline)
# Preserves your original strategy logic and replaces Algotest with Flattrade.
# MTM is sourced from Flattrade PositionBook (rpnl + urmtom for this strategy's symbols).

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
STRADDLE_LOTS = 4            # lots to trade per entry (per leg)
LOT_SIZE_MAP = {"NIFTY": 65} # units per lot

# Time windows
START_TIME_STR = "10:45"     # HH:MM 24h
EXIT_TIME_STR  = "15:14"     # HH:MM 24h

# PCT watch windows (optional - leave blank to disable watch behavior)
# Start watch: After PCT_START_TIME_STR and before START_TIME_STR, strategy is in watch mode and will only
# enter early if ATM CE/PE percent-diff <= PCT_START_DIFF_PCT (interchanged semantics: "less than or equal").
# Otherwise it enters normally at START_TIME_STR.
PCT_START_TIME_STR = "10:35"      # e.g., "09:16" or "" to disable
PCT_START_DIFF_PCT = 1.0     # e.g., 2.0. If 0 or time is "", start watch is disabled.

# End watch: After PCT_END_TIME_STR and before EXIT_TIME_STR, strategy remains active and keeps running normal logic,
# but if ATM CE/PE percent-diff >= PCT_END_DIFF_PCT (interchanged semantics: "greater than or equal"),
# it will exit immediately and terminate for the day.
PCT_END_TIME_STR = "14:50"        # e.g., "14:45" or "" to disable
PCT_END_DIFF_PCT = 30.0       # e.g., 30.0. If 0 or time is "", end watch is disabled.

# Strategy flags
REFRESH_INTERVAL = 10        # seconds
HOLD_ENABLED = False
HOLD_TIME_MINUTES = 1
REQUIRE_ATM_CHANGE = True

# Stop/Target thresholds (per-lot)
STOPLOSS_PER_LOT = 2000.0
TARGET_PER_LOT   = 6000.0

# NEW flag:
# If True, stoploss/target are applied to TOTAL MTM (account-wide).
# If False (default), stoploss/target apply to STRATEGY MTM (running + closed positions of the strategy scope).
STOP_TARGET_ON_TOTAL = False

# NEW flag:
# If True (default), when the script starts it will "baseline" existing PositionBook values
# and compute Strategy MTM as the change since script start (fresh MTM). This prevents previously
# closed P&L from terminating the strategy immediately. If False, strategy MTM will include historical closed positions.
IGNORE_PAST_STRATEGY_POSITIONS = True

# Logging style
LOG_STYLE = "classic"        # "classic" or "single"
SHOW_MTM_ROWS = True         # True: show per-symbol netqty|rpnl|urmtom rows



# Flattrade credentials (update JKEY daily)
FT_UID   = "FZ21703"
FT_ACTID = "FZ21703"
FT_JKEY  = ""

# override FT_UID/FT_ACTID/FT_JKEY from flattrade token file if available
try:
    import os, json
    _tf = os.getenv("FLATTRADE_TOKEN_FILE", "/home/opc/algo/flattrade/prime/Nifty/flattrade_token_headless.json")
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
# Option 1 (simple): paste today's access token
# Option 1: manual daily access token (leave blank to load from file)
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

def _parse_hhmm_optional(s: str) -> Optional[datetime.time]:
    try:
        s = (s or "").strip()
        if not s:
            return None
        h, m = s.split(":")
        return datetime.time(int(h), int(m))
    except Exception:
        return None

START_TIME = _parse_hhmm(START_TIME_STR)
EXIT_TIME  = _parse_hhmm(EXIT_TIME_STR)
PCT_START_TIME = _parse_hhmm_optional(PCT_START_TIME_STR)
PCT_END_TIME   = _parse_hhmm_optional(PCT_END_TIME_STR)

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
# Margin detection helpers (minimal, additive)
# - These are intentionally small and non-invasive.
# =====================
_MARGIN_KEYWORDS = [
    "margin", "shortfall", "insufficient", "insufficient funds",
    "not enough margin", "not enough funds", "funds insufficient",
    "check cash ratio", "cash ratio"
]

def _extract_broker_message(resp_or_text) -> str:
    """
    Return a best-effort human message from either a response dict or string.
    Works with flattrade responses that use keys like 'emsg','message','error','err'.
    """
    try:
        if isinstance(resp_or_text, dict):
            for k in ("emsg", "message", "error", "err", "stat", "status"):
                v = resp_or_text.get(k)
                if v:
                    return str(v)
            # fallback: return textual representation of dict
            return str(resp_or_text)
        else:
            # if it's already text
            return str(resp_or_text)
    except Exception:
        return str(resp_or_text)

def _is_margin_error(resp_or_text) -> bool:
    """
    Return True if any known margin keyword appears in the provided response text or dict.
    """
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

def ce_pe_pct_diff(ce_ltp: float, pe_ltp: float) -> float:
    """
    Percent difference between CE and PE LTPS based on average:
      pct = 100 * |CE - PE| / ((CE + PE)/2)    when both > 0
    Returns 0.0 if any leg has non-positive LTP.
    """
    try:
        if ce_ltp > 0.0 and pe_ltp > 0.0:
            return abs(ce_ltp - pe_ltp) / max(1e-9, (ce_ltp + pe_ltp) / 2.0) * 100.0
        return 0.0
    except Exception:
        return 0.0

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
# FLATTRADE TRANSPORT (proven: form-encoded jData JSON + jKey)
# =====================

def ft_post(endpoint: str, jdata: dict) -> dict:
    """
    Flattrade Pi: send jData JSON string and jKey as form fields (x-www-form-urlencoded).
    """
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
    """
    PlaceOrder wrapper (tsym should be Flattrade trading symbol). For MKT, prc must be "0".
    """
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
    """
    Returns list of positions from Flattrade PositionBook, or [] on failure.
    """
    jdata = {"uid": FT_UID, "actid": FT_ACTID}
    resp = ft_post("PositionBook", jdata)
    return resp if isinstance(resp, list) else []

# =====================
# SYMBOL HELPERS (internal symbol ↔ Flattrade tsym)
# Ensure these are defined before StrategyContext to avoid NameErrors
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
        return dt.strftime("%y%mdd")
    except Exception:
        return raw_expiry

def build_option_symbol(symbol: str, expiry_raw: str, strike: int, opt_type: str) -> str:
    # Internal key used by strategy state: SYMBOL + yymmdd + [C|P] + strike
    yymmdd = expiry_to_yymmdd(expiry_raw)
    strike_str = str(int(strike))
    return f"{symbol}{yymmdd}{opt_type}{strike_str}"

def extract_strike_from_symbol(instrument: str) -> Optional[int]:
    m = re.search(r'^[A-Z]+(\d{6})[CP](\d+)$', instrument)
    if not m:
        return None
    try:
        return int(m.group(2))
    except Exception:
        return None

def tsym_from_internal(instrument: str) -> str:
    """
    Convert internal instrument (e.g., NIFTYyymmddC26150) to Flattrade tsym NIFTYddMONyyC26150 using EXPIRY_DATE.
    """
    m = re.fullmatch(r'^([A-Z]+)(\d{6})([CP])(\d+)$', instrument)
    if not m:
        return instrument
    sym, yymmdd, opt, strike = m.groups()
    # FIX: derive ddMONyy from the instrument's embedded yymmdd rather than global EXPIRY_DATE for accuracy
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
# These must be present before main_loop
# =====================

def upstox_refresh_access_token() -> Optional[str]:
    """
    Refresh Upstox access token using OAuth2 refresh_token flow (if credentials are set).
    Returns new access_token or None.
    """
    if not (UPSTOX_CLIENT_ID and UPSTOX_CLIENT_SECRET and UPSTOX_REFRESH_TOKEN):
        print(f"[{nowstr()}] ⚠️ Upstox refresh skipped: missing client/secret/refresh_token")
        return None
    data = {
        "grant_type": "refresh_token",
        "client_id": UPSTOX_CLIENT_ID,
        "client_secret": UPSTOX_CLIENT_SECRET,
        "refresh_token": UPSTOX_REFRESH_TOKEN,
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
        print(f"[{nowstr()}] ⚠️ Upstox refresh non-JSON: {r.text[:200]}")
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

    # First attempt
    tok = UPSTOX_ACCESS_TOKEN or None
    try:
        resp = do_call(tok)
    except Exception as e:
        print(f"[{nowstr()}] ⚠️ Upstox connection error: {e}")
        return []

    # If token invalid, try refresh once if creds are provided
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

# =====================
# Dynamic expiry probe (added – preserves all original logic; only augments expiry handling)
# =====================

def find_nearest_expiry_by_probe(max_days: int = 14) -> Optional[str]:
    """
    Probe upcoming business days (default window = max_days) and return the first
    expiry date (YYYY-MM-DD) for which the Upstox option chain API returns a non-empty list.
    Returns None if no expiry found within the window.
    """
    today = datetime.date.today()
    for i in range(0, max_days + 1):
        d = today + datetime.timedelta(days=i)
        if d.weekday() >= 5:  # skip Saturday(5) & Sunday(6)
            continue
        expiry = d.strftime("%Y-%m-%d")
        try:
            data = get_option_chain_from_upstox(expiry)
            if isinstance(data, list) and len(data) > 0:
                return expiry
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 404):
                continue
            continue
        except Exception:
            continue
    return None

# =====================
# STRATEGY MTM / ACCOUNT MTM helpers (must be present before main loop)
# =====================

def compute_leg_mtm_from_rows(ctx: "StrategyContext", rows: List[Dict[str, Any]]) -> Tuple[float, float]:
    """
    Find CE and PE rows corresponding to the context's instruments and return
    (ce_mtm, pe_mtm) where each is rpnl + urmtom (urmtom assumed already adjusted).
    Returns 0.0 for a leg if row not found.
    """
    ce_mtm = 0.0
    pe_mtm = 0.0
    ce_instr = build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.ce_strike, "C") if ctx.ce_strike else None
    pe_instr = build_option_symbol(SYMBOL, SELECTED_EXPIRY, ctx.pe_strike, "P") if ctx.pe_strike else None
    tsym_ce = (ctx.tsym_map.get(ce_instr) if ce_instr else None) or (tsym_from_internal(ce_instr) if ce_instr else None)
    tsym_pe = (ctx.tsym_map.get(pe_instr) if pe_instr else None) or (tsym_from_internal(pe_instr) if pe_instr else None)
    if tsym_ce:
        tsym_ce = str(tsym_ce).upper()
    if tsym_pe:
        tsym_pe = str(tsym_pe).upper()
    for r in rows:
        try:
            ts = str(r.get("tsym", "")).upper()
            netqty = int(float(r.get("netqty", 0)))
            rpnl = float(r.get("rpnl", 0.0))
            ur = float(r.get("urmtom", 0.0)) if netqty != 0 else 0.0
            if tsym_ce and ts == tsym_ce:
                ce_mtm = rpnl + ur
            if tsym_pe and ts == tsym_pe:
                pe_mtm = rpnl + ur
        except Exception:
            continue
    return ce_mtm, pe_mtm

def compute_strategy_mtms(ctx: "StrategyContext") -> Tuple[float, float]:
    """
    Compute CE_total and PE_total across PositionBook rows for the strategy's scoped instruments.
    Includes running + closed positions. Subtracts baseline when configured.
    """
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
    """
    Sum rpnl + urmtom for all rows in PositionBook (account-wide).
    """
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

# --------------------
# Helper: detect option type from tsym
# --------------------
def _detect_option_type_from_tsym(ts: str) -> Optional[str]:
    """
    Return "C" or "P" if tsym indicates call/put. Uses regex to match CE/PE or single C/P before the strike digits.
    Examples matched:
      - NIFTY04NOV25C25750  -> "C"
      - NIFTY04NOV25CE25750 -> "C"
      - NIFTY04NOV25PE25750 -> "P"
      - NIFTY04NOV25P25750  -> "P"
    """
    if not ts:
        return None
    m = re.search(r'(CE|PE|C|P)(\d{3,6})$', ts, flags=re.IGNORECASE)
    if not m:
        return None
    op = m.group(1).upper()
    if op.startswith('C'):
        return "C"
    if op.startswith('P'):
        return "P"
    return None

# =====================
# Verify / Safety helpers (used by StrategyContext)
# =====================

def _broker_netqty_for_tsym(tsym: str) -> int:
    try:
        if not tsym:
            return 0
        pb = flattrade_fetch_positionbook()
        if not pb:
            return 0
        ts = str(tsym).upper()
        for r in pb:
            try:
                if str(r.get("tsym", "")).upper() == ts:
                    return int(float(r.get("netqty", 0)))
            except Exception:
                continue
    except Exception:
        pass
    return 0

def verify_entry_and_squareoff_if_needed(ctx: "StrategyContext", ce_internal: str, pe_internal: str, logfile_path: str, wait_seconds: float = 1.0) -> bool:
    """
    Verify PositionBook shows expected netqty for CE and PE after an attempted entry.
    If either leg is not fully net-short (exact expected size), emergency-exit any filled legs,
    mark ctx.terminated and return False. Otherwise return True.
    """
    try:
        # resolve Flattrade tsym names once
        ts_ce = str(ctx.tsym_map.get(ce_internal) or tsym_from_internal(ce_internal)).upper() if ce_internal else None
        ts_pe = str(ctx.tsym_map.get(pe_internal) or tsym_from_internal(pe_internal)).upper() if pe_internal else None

        # expected absolute units per leg
        expected_units = -int(STRADDLE_LOTS) * int(get_lot_size(SYMBOL))

        # poll for a short window to allow broker to reflect fills
        deadline = time.time() + max(1.0, float(wait_seconds))
        last_ce = last_pe = None
        while time.time() < deadline:
            net_ce = _broker_netqty_for_tsym(ts_ce) if ts_ce else 0
            net_pe = _broker_netqty_for_tsym(ts_pe) if ts_pe else 0
            last_ce, last_pe = net_ce, net_pe
            if net_ce == expected_units and net_pe == expected_units:
                return True
            time.sleep(0.3)

        # Final read after polling
        net_ce = _broker_netqty_for_tsym(ts_ce) if ts_ce else 0
        net_pe = _broker_netqty_for_tsym(ts_pe) if ts_pe else 0

        # If either leg is not at the exact expected short size, treat as failed entry.
        if net_ce != expected_units or net_pe != expected_units:
            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Entry verification mismatch:"
                    f" CE netqty={net_ce} (exp {expected_units}) | PE netqty={net_pe} (exp {expected_units})."
                    f" Emergency square-off of any filled legs.")
            ctx.log_to_file(logfile_path,
                f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Entry verification mismatch:"
                f" CE netqty={net_ce} (exp {expected_units}) | PE netqty={net_pe} (exp {expected_units})."
                f" Emergency square-off of any filled legs.")

            # emergency exit any partially-filled positions belonging to this context
            for instr in list(ctx.positions.keys()):
                try:
                    ctx.exit_instrument_until_success(instr, label="entry-mismatch-exit")
                except Exception as e:
                    ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Emergency exit failed for {instr}: {e}")
                    ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Emergency exit failed for {instr}: {e}")

            # clear local state and prevent re-entry
            ctx.in_position = False
            ctx.ce_strike = ctx.pe_strike = None
            ctx.ce_entry_price = ctx.pe_entry_price = None
            ctx.terminated = True
            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after entry mismatch.")
            ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after entry mismatch.")
            return False

        # exact match
        return True

    except Exception as ex:
        ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Exception during entry verification: {ex}")
        ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ Exception during entry verification: {ex}")
        return False

# =====================
# BROKER MTM (PositionBook) scoped to this strategy
# Provides the broker-truth MTM rows used by logging
# =====================

def broker_mtm_for_ctx(ctx: "StrategyContext") -> Tuple[float, float, float, List[Dict[str, Any]]]:
    """
    Returns (total_mtm, sum_rpnl, sum_ur, rows[]) scoped to this context's instruments.
    Each row: {tsym, netqty, rpnl, urmtom}.
    """
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
            rows.append({"tsym": ts, "netqty": netqty, "rpnl": rpnl, "urmtom": ur})
            sum_rpnl += rpnl
            sum_ur += ur
        except Exception:
            continue
    return sum_rpnl + sum_ur, sum_rpnl, sum_ur, rows

# =====================
# BROKER SEND HELPERS (Flattrade) - return raw resp for inspection
# =====================

def flattrade_response_to_tuple(resp: dict) -> Tuple[bool, Optional[int], str, dict]:
    """
    Returns (ok, status_placeholder, text_message, raw_resp_dict)
    """
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

def broker_send_sell(_unused_webhook_url: str, instrument: str, lots: int, label: str):
    tsym = tsym_from_internal(instrument)
    qty_units = max(1, int(lots) * get_lot_size(SYMBOL))
    resp = flattrade_place_order(tsym=tsym, qty_units=qty_units, trantype="S", prctyp="MKT", prc=None, label=label)
    ok, status, text, raw = flattrade_response_to_tuple(resp)
    return (ok, status, text, raw), tsym

def broker_send_buy(_unused_webhook_url: str, instrument: str, lots: int, label: str):
    tsym = tsym_from_internal(instrument)
    qty_units = max(1, int(lots) * get_lot_size(SYMBOL))
    resp = flattrade_place_order(tsym=tsym, qty_units=qty_units, trantype="B", prctyp="MKT", prc=None, label=label)
    ok, status, text, raw = flattrade_response_to_tuple(resp)
    return (ok, status, text, raw), tsym

# =====================
# LOGGING HELPERS
# =====================

def log_atm_line(ctx_pct: float, spot: float, atm: int, ce_strike: Optional[int], ce_ltp: float, pe_strike: Optional[int], pe_ltp: float, expiry: str) -> str:
    return f"[{nowstr()}] [PCT={ctx_pct:g}%] 📊 Spot={spot:.2f} | ATM={atm} | CE ATM={ce_strike} | CE={ce_ltp:.2f} | PE ATM={pe_strike} | PE={pe_ltp:.2f} | Expiry={expiry}"

def log_mtm_row(ctx_pct: float, ts: str, netqty: int, rpnl: float, ur: float) -> str:
    return f"[{nowstr()}] [PCT={ctx_pct:g}%] 💰 {ts} | netqty={netqty} | rpnl={rpnl:.2f} | urmtom={ur:.2f}"

def log_mtm_total(ctx_pct: float, total: float, rpnl: float, ur: float) -> str:
    return f"[{nowstr()}] [PCT={ctx_pct:g}%] 💰 MTM={total:.2f} (rpnl={rpnl:.2f}, urmtom={ur:.2f})"

def log_change_line(ctx_pct: float, ce_change: float, pe_change: float) -> str:
    return f"[{nowstr()}] [PCT={ctx_pct:g}%] 🔍 CE Change={ce_change:+.2f}% | PE Change={pe_change:+.2f}%"

# =====================
# STRATEGY CONTEXT (preserved, added terminated flag)
# All logic preserved; only small, well-scoped safety additions were placed above.
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
        # NEW: when True, the context will not enter again for this run (stop/target hit)
        self.terminated: bool = False
        # NEW: one-time announce flag for PCT start watch
        self.start_watch_announced: bool = False

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
        Called when an order failure indicates a margin shortfall.
        Terminates the context for the day and attempts a best-effort one-shot exit
        of all known positions using broker_send_buy (no retry loops).
        """
        try:
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ❌ Margin shortfall detected: {reason_text}. Initiating emergency exit of all strategy positions and terminating context for the day.")
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] 🔍 Full broker response: {raw_resp}")
            # Mark terminated so no new entries will be attempted
            self.terminated = True
            # Attempt one-shot exits for all known positions (best-effort)
            for instr in list(self.positions.keys()):
                try:
                    qty = self.positions.get(instr, {}).get("quantity", STRADDLE_LOTS)
                    (ok_exit, _, text_exit, raw_exit), tsym = broker_send_buy(self.webhook_url, instr, qty, label="margin-exit")
                    if ok_exit:
                        self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ✅ Emergency exit placed for {instr} (tsym={tsym}).")
                        # remove local tracking for this instrument
                        self.positions.pop(instr, None)
                        self.ledger.add(instr)
                    else:
                        self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Emergency exit FAILED for {instr}: {text_exit} | raw={raw_exit}")
                except Exception as e:
                    self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Exception while emergency exiting {instr}: {e}")
            # clear strikes/baselines for safety
            self.ce_strike = None
            self.pe_strike = None
            self.ce_entry_price = None
            self.pe_entry_price = None
            self.in_position = False
        except Exception as e:
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Error in handle_margin_shortfall: {e}")

    def exit_instrument_until_success(self, instrument: str, entry_price_for_mtm: Optional[float] = None, label: str = "exit"):
        """
        Before placing a buy (exit) order, check broker PositionBook for that instrument's netqty.
        If broker already shows netqty == 0 for that tsym, treat as already closed and clean local state
        without placing a buy. This prevents creating an unintended buy if the leg was never filled.
        """
        # resolve tsym as broker sees it
        tsym = self.tsym_map.get(instrument) or tsym_from_internal(instrument)
        broker_net = _broker_netqty_for_tsym(tsym) if tsym else None
        if broker_net == 0:
            # already closed on broker side; update local state and return
            self.positions.pop(instrument, None)
            self.ledger.add(instrument)
            if instrument.endswith("C"):
                self.ce_strike = None; self.ce_entry_price = None
            elif instrument.endswith("P"):
                self.pe_strike = None; self.pe_entry_price = None
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ℹ️ Broker already shows netqty=0 for {tsym}; skipping exit order and clearing local tracking.")
            return True

        lots = self.positions.get(instrument, {}).get("quantity", STRADDLE_LOTS)
        attempt = 1
        while True:
            (ok, status, text, raw), tsym_resp = broker_send_buy(self.webhook_url, instrument, lots, f"{label} (attempt {attempt})")
            if ok:
                self.positions.pop(instrument, None)
                if instrument.endswith("C"):
                    self.ce_strike = None; self.ce_entry_price = None
                elif instrument.endswith("P"):
                    self.pe_strike = None; self.pe_entry_price = None
                self.tsym_map[instrument] = tsym_resp
                self.ledger.add(instrument)
                return True
            # detect margin-like errors and abort by exiting all positions
            if raw and _is_margin_error(raw):
                self.handle_margin_shortfall(_extract_broker_message(raw), raw)
                return False
            if text and _is_margin_error(text):
                self.handle_margin_shortfall(_extract_broker_message(text))
                return False
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ❌ {label} failed; retrying in 3s... ({text})")
            time.sleep(3); attempt += 1

    def enter_instrument_until_success(self, instrument: str, entry_price: Optional[float] = None, label: str = "entry"):
        if instrument in self.positions and self.positions[instrument].get("side") == "S":
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ⚠️ Skipping duplicate sell for {instrument} (already short locally).")
            return True
        lots = STRADDLE_LOTS
        attempt = 1
        while True:
            (ok, status, text, raw), tsym = broker_send_sell(self.webhook_url, instrument, lots, f"{label} (attempt {attempt})")
            if ok:
                self.positions[instrument] = {"side": "S", "entry_price": entry_price if entry_price is not None else 0.0, "quantity": lots, "mtm": 0.0}
                strike_extracted = extract_strike_from_symbol(instrument)
                if instrument.endswith("C"):
                    self.ce_strike = strike_extracted; self.ce_entry_price = entry_price
                elif instrument.endswith("P"):
                    self.pe_strike = strike_extracted; self.pe_entry_price = entry_price
                self.tsym_map[instrument] = tsym
                self.ledger.add(instrument)
                return True
            # detect margin-like errors and abort by exiting all positions
            if raw and _is_margin_error(raw):
                self.handle_margin_shortfall(_extract_broker_message(raw), raw)
                return False
            if text and _is_margin_error(text):
                self.handle_margin_shortfall(_extract_broker_message(text))
                return False
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ❌ {label} failed; retrying in 3s... ({text})")
            time.sleep(3); attempt += 1

    def enter_straddle_until_success(self, ce_strike: int, pe_strike: int, expiry_raw: str, ce_entry_price: Optional[float] = None, pe_entry_price: Optional[float] = None, lots: int = STRADDLE_LOTS):
        attempt = 1
        while True:
            ce_instr = build_option_symbol(SYMBOL, expiry_raw, ce_strike, "C")
            pe_instr = build_option_symbol(SYMBOL, expiry_raw, pe_strike, "P")
            (ok_ce, _, text_ce, raw_ce), tsym_ce = broker_send_sell(self.webhook_url, ce_instr, lots, f"entry-straddle-CE (attempt {attempt})")
            (ok_pe, _, text_pe, raw_pe), tsym_pe = broker_send_sell(self.webhook_url, pe_instr, lots, f"entry-straddle-PE (attempt {attempt})")
            if ok_ce and ok_pe:
                self.positions[ce_instr] = {"side": "S", "entry_price": ce_entry_price if ce_entry_price is not None else 0.0, "quantity": lots, "mtm": 0.0}
                self.positions[pe_instr] = {"side": "S", "entry_price": pe_entry_price if pe_entry_price is not None else 0.0, "quantity": lots, "mtm": 0.0}
                self.ce_strike = ce_strike; self.ce_entry_price = ce_entry_price
                self.pe_strike = pe_strike; self.pe_entry_price = pe_entry_price
                self.tsym_map[ce_instr] = tsym_ce
                self.tsym_map[pe_instr] = tsym_pe
                self.ledger.add(ce_instr); self.ledger.add(pe_instr)
                return True
            # If either returned a margin error, terminate and emergency-exit
            combined_raw = {}
            if isinstance(raw_ce, dict):
                combined_raw.update(raw_ce)
            if isinstance(raw_pe, dict):
                combined_raw.update(raw_pe)
            combined_text = " ".join(filter(None, [str(text_ce or ""), str(text_pe or "")]))
            if (combined_raw and _is_margin_error(combined_raw)) or (combined_text and _is_margin_error(combined_text)):
                # prefer raw if available
                self.handle_margin_shortfall(_extract_broker_message(combined_raw) or _extract_broker_message(combined_text), combined_raw or None)
                return False
            self.log(f"[{nowstr()}] [PCT={self.trigger_pct:g}%] ❌ entry-straddle failed; retrying in 3s... (CE:{text_ce} | PE:{text_pe})")
            time.sleep(3); attempt += 1

# =====================
# MAIN LOOP
# =====================

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
                        # Only baseline instrument rows that pertain to the SYMBOL to avoid unrelated instruments
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

    # Determine earliest activation time (either PCT start time if it comes earlier than START_TIME, or START_TIME)
    earliest_times: List[datetime.time] = [START_TIME]
    if PCT_START_TIME is not None:
        earliest_times.append(PCT_START_TIME)
    earliest_active_time = min(earliest_times)

    # flags for watch features
    start_watch_enabled = (PCT_START_TIME is not None) and (PCT_START_DIFF_PCT > 0.0)
    end_watch_enabled   = (PCT_END_TIME is not None) and (PCT_END_DIFF_PCT > 0.0)

    try:
        while True:
            now = datetime.datetime.now()

            # Wait until earliest of PCT_START_TIME (if configured) or START_TIME
            if now.time() < earliest_active_time:
                if start_watch_enabled and PCT_START_TIME and PCT_START_TIME <= START_TIME:
                    print(f"[{nowstr()}] ⏳ Waiting for PCT start watch at {PCT_START_TIME}")
                    write_logfile_entry(logfile_path, f"[{nowstr()}] ⏳ Waiting for PCT start watch at {PCT_START_TIME}\n")
                else:
                    print(f"[{nowstr()}] ⏳ Waiting for market open at {START_TIME}")
                    write_logfile_entry(logfile_path, f"[{nowstr()}] ⏳ Waiting for market open at {START_TIME}\n")
                time.sleep(10)
                continue

            # Hard EXIT time: close everything and end the loop
            if now.time() >= EXIT_TIME:
                for ctx in strategies_config:
                    if ctx.in_position:
                        ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 Exit time reached. Closing positions...")
                        ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 Exit time reached. Closing positions...")
                        for instr in list(ctx.positions.keys()):
                            ctx.exit_instrument_until_success(instr, label="exit-eod")
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

            for ctx in strategies_config:
                try:
                    spot, atm_strike, best_item = pick_atm_from_chain(data)
                    if atm_strike is None:
                        msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ⚠️ No ATM determined from chain, skipping this cycle."
                        ctx.log(msg); ctx.log_to_file(logfile_path, msg + "\n\n")
                        print(); write_logfile_entry(logfile_path, "\n\n")
                        continue

                    # =========
                    # PCT START WATCH LOGIC (enter only when diff <= PCT_START_DIFF_PCT during watch window; interchanged semantics)
                    # =========
                    # If not in position yet (and not terminated), handle start-watch or normal entry window
                    if not ctx.in_position and not ctx.terminated:
                        # Are we inside the PCT start watch window?
                        if start_watch_enabled and (PCT_START_TIME <= now.time() < START_TIME):
                            # one-time announce of PCT start trigger per context
                            if not ctx.start_watch_announced:
                                ann = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] PCT start time triggered ,"
                                ctx.log(ann); ctx.log_to_file(logfile_path, ann)
                                ctx.start_watch_announced = True

                            ce0 = get_leg_ltp_from_chain(data, atm_strike, "C")
                            pe0 = get_leg_ltp_from_chain(data, atm_strike, "P")
                            start_diff = ce_pe_pct_diff(ce0, pe0)

                            # Existing informative line (preserved)
                            msg = (f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 👀 PCT-START watch window active "
                                   f"({PCT_START_TIME_STR}→{START_TIME_STR}). ATM diff={start_diff:.2f}% "
                                   f"(threshold={PCT_START_DIFF_PCT:.2f}%).")
                            ctx.log(msg); ctx.log_to_file(logfile_path, msg)

                            # New awaiting-style log line requested
                            aw = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] awaiting of min pct diff of ce pe ( diff is {start_diff:.0f} % )"
                            ctx.log(aw); ctx.log_to_file(logfile_path, aw)

                            if start_diff <= PCT_START_DIFF_PCT:
                                # Threshold met before START_TIME — enter now
                                met1 = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] pct diff condition ce pe met ."
                                met2 = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] entering the straddle."
                                ctx.log(met1); ctx.log_to_file(logfile_path, met1)
                                ctx.log(met2); ctx.log_to_file(logfile_path, met2)

                                ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ✅ PCT-START threshold met; entering initial straddle early...")
                                ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ✅ PCT-START threshold met; entering initial straddle early...")
                                ctx.enter_straddle_until_success(atm_strike, atm_strike, NEAREST_EXPIRY, ce_entry_price=ce0, pe_entry_price=pe0, lots=STRADDLE_LOTS)
                                ctx.last_roll_time = now
                                ctx.in_position = True
                                # verify both legs filled; if not, emergency-squareoff and terminate context
                                ce_instr = build_option_symbol(SYMBOL, SELECTED_EXPIRY, atm_strike, "C")
                                pe_instr = build_option_symbol(SYMBOL, SELECTED_EXPIRY, atm_strike, "P")
                                ok = verify_entry_and_squareoff_if_needed(ctx, ce_instr, pe_instr, logfile_path, wait_seconds=1.0)
                                if not ok:
                                    # verification handled emergency exit and termination
                                    continue
                                write_logfile_entry(logfile_path, "\n\n")
                            else:
                                # Keep watching until START_TIME or until threshold triggers
                                write_logfile_entry(logfile_path, "\n")
                                # Skip further entry processing this cycle
                                continue
                        elif now.time() >= START_TIME:
                            # Normal initial entry (unchanged logic)
                            msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🚀 Entering initial straddle..."
                            ctx.log(msg); ctx.log_to_file(logfile_path, msg)
                            ce0 = get_leg_ltp_from_chain(data, atm_strike, "C")
                            pe0 = get_leg_ltp_from_chain(data, atm_strike, "P")
                            # attempt entry using the dynamically selected expiry
                            ctx.enter_straddle_until_success(atm_strike, atm_strike, NEAREST_EXPIRY, ce_entry_price=ce0, pe_entry_price=pe0, lots=STRADDLE_LOTS)
                            ctx.last_roll_time = now
                            ctx.in_position = True
                            # verify both legs filled; if not, emergency-squareoff and terminate context
                            ce_instr = build_option_symbol(SYMBOL, SELECTED_EXPIRY, atm_strike, "C")
                            pe_instr = build_option_symbol(SYMBOL, SELECTED_EXPIRY, atm_strike, "P")
                            ok = verify_entry_and_squareoff_if_needed(ctx, ce_instr, pe_instr, logfile_path, wait_seconds=1.0)
                            if not ok:
                                # verification handled emergency exit and termination
                                continue
                            write_logfile_entry(logfile_path, "\n\n")
                            # Continue to next ctx to let normal cycle proceed
                            continue
                        else:
                            # Before any start time; should not happen due to earliest gating, but keep safe
                            continue

                    # Current LTPs for held legs (context)
                    ce_ltp = get_leg_ltp_from_chain(data, ctx.ce_strike, "C") if ctx.ce_strike else 0.0
                    pe_ltp = get_leg_ltp_from_chain(data, ctx.pe_strike, "P") if ctx.pe_strike else 0.0

                    # Broker-truth MTM details (running positions)
                    total_mtm, sum_rpnl, sum_ur, rows = broker_mtm_for_ctx(ctx)

                    # STRATEGY MTM: includes running positions + closed positions for strategy-scoped instruments
                    strat_ce_total, strat_pe_total = compute_strategy_mtms(ctx)
                    strat_total = strat_ce_total + strat_pe_total

                    # ACCOUNT TOTAL MTM (all positions in account)
                    account_total = compute_account_total_mtm()

                    # Print Strategy MTM and TOTAL MTM lines (Strategy first, then TOTAL)
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
                                line = log_mtm_row(ctx.trigger_pct, r["tsym"], r["netqty"], r["rpnl"], r["urmtom"])
                                ctx.log(line); ctx.log_to_file(logfile_path, line)
                        # === two blank lines after each tick ===
                        print(); print()
                        write_logfile_entry(logfile_path, "\n\n")
                    else:
                        l1 = log_atm_line(ctx.trigger_pct, spot, atm_strike, ctx.ce_strike, ce_ltp, ctx.pe_strike, pe_ltp, NEAREST_EXPIRY)
                        ctx.log(l1); ctx.log_to_file(logfile_path, l1)
                        if SHOW_MTM_ROWS and rows:
                            for r in rows:
                                l2 = log_mtm_row(ctx.trigger_pct, r["tsym"], r["netqty"], r["rpnl"], r["urmtom"])
                                ctx.log(l2); ctx.log_to_file(logfile_path, l2)
                        else:
                            l2 = log_mtm_total(ctx.trigger_pct, total_mtm, sum_rpnl, sum_ur)
                            ctx.log(l2); ctx.log_to_file(logfile_path, l2)
                        ce_change = ((ce_ltp - (ctx.ce_entry_price or 0.0)) / (ctx.ce_entry_price or 1.0) * 100.0) if (ctx.ce_entry_price or 0.0) else 0.0
                        pe_change = ((pe_ltp - (ctx.pe_entry_price or 0.0)) / (ctx.pe_entry_price or 1.0) * 100.0) if (ctx.pe_entry_price or 0.0) else 0.0
                        l3 = log_change_line(ctx.trigger_pct, ce_change, pe_change)
                        ctx.log(l3); ctx.log_to_file(logfile_path, l3)
                        # two blank lines for readability
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
                                ctx.exit_instrument_until_success(instr, label="stoploss exit")
                            ctx.in_position = False
                            ctx.ce_strike = ctx.pe_strike = None
                            ctx.ce_entry_price = ctx.pe_entry_price = None
                            # NEW: prevent any further entries for this context for the running session
                            ctx.terminated = True
                            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after stoploss on {metric_name}.")
                            ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after stoploss on {metric_name}.")
                            continue
                        elif check_val >= TARGET_PER_LOT * STRADDLE_LOTS:
                            msg = f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🎯 {metric_name} target hit! Exiting all positions..."
                            ctx.log(msg); ctx.log_to_file(logfile_path, msg)
                            for instr in list(ctx.positions.keys()):
                                ctx.exit_instrument_until_success(instr, label="target exit")
                            ctx.in_position = False
                            ctx.ce_strike = ctx.pe_strike = None
                            ctx.ce_entry_price = ctx.pe_entry_price = None
                            # NEW: prevent re-entry for this context
                            ctx.terminated = True
                            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after target on {metric_name}.")
                            ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ℹ️ Context terminated for the day after target on {metric_name}.")
                            continue

                    # =========
                    # PCT END WATCH LOGIC (exit when diff >= PCT_END_DIFF_PCT during watch window; interchanged semantics)
                    # =========
                    if ctx.in_position and end_watch_enabled and (PCT_END_TIME <= now.time() < EXIT_TIME):
                        ce_atm = get_leg_ltp_from_chain(data, atm_strike, "C")
                        pe_atm = get_leg_ltp_from_chain(data, atm_strike, "P")
                        end_diff = ce_pe_pct_diff(ce_atm, pe_atm)
                        msg = (f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 👀 PCT-END watch window active "
                               f"({PCT_END_TIME_STR}→{EXIT_TIME_STR}). ATM diff={end_diff:.2f}% "
                               f"(threshold={PCT_END_DIFF_PCT:.2f}%).")
                        ctx.log(msg); ctx.log_to_file(logfile_path, msg)
                        if end_diff >= PCT_END_DIFF_PCT:
                            # Exit immediately and terminate for the day
                            ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ✅ PCT-END threshold met; exiting all positions for the day...")
                            ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] ✅ PCT-END threshold met; exiting all positions for the day...")
                            for instr in list(ctx.positions.keys()):
                                ctx.exit_instrument_until_success(instr, label="pct-end exit")
                            ctx.in_position = False
                            ctx.ce_strike = ctx.pe_strike = None
                            ctx.ce_entry_price = ctx.pe_entry_price = None
                            # Prevent re-entry for the day
                            ctx.terminated = True
                            # proceed to next context
                            continue

                    # Trigger/hold/ATM-change logic (preserved)
                    ce_change_pct = ((ce_ltp - (ctx.ce_entry_price or 0.0)) / (ctx.ce_entry_price or 1.0) * 100.0) if (ctx.ce_entry_price or 0.0) else 0.0
                    pe_change_pct = ((pe_ltp - (ctx.pe_entry_price or 0.0)) / (ctx.pe_entry_price or 1.0) * 100.0) if (ctx.pe_entry_price or 0.0) else 0.0
                    triggered_ce = abs(ce_change_pct) >= ctx.trigger_pct
                    triggered_pe = abs(pe_change_pct) >= ctx.trigger_pct
                    hold_ok = (not HOLD_ENABLED) or ((now - ctx.last_roll_time) >= HOLD_TIME if ctx.last_roll_time else True)
                    if REQUIRE_ATM_CHANGE:
                        ce_atm_changed = atm_strike != ctx.ce_strike
                        pe_atm_changed = atm_strike != ctx.pe_strike
                    else:
                        ce_atm_changed = pe_atm_changed = True
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
                                ctx.exit_instrument_until_success(ce_exit_instr, label="exit-partial-CE")
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
                                ctx.exit_instrument_until_success(pe_exit_instr, label="exit-partial-PE")
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
        write_logfile_entry(logfile_path, f"[{nowstr()}] ⚠️ KeyboardInterrupt detected — attempting graceful shutdown for all strategies...\n")
        for ctx in strategies_config:
            try:
                if ctx.positions:
                    ctx.log(f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 Sending manual exit for all positions...")
                    ctx.log_to_file(logfile_path, f"[{nowstr()}] [PCT={ctx.trigger_pct:g}%] 🛑 Sending manual exit for all positions...")
                    # iterate copy because exit_instrument_until_success removes entries as they close
                    for instr in list(ctx.positions.keys()):
                        ctx.exit_instrument_until_success(instr, label="Manual Exit")
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
