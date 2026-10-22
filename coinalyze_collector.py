#!/usr/bin/env python3
"""Coinalyze public market data collector: BTC/ETH Binance USDT perpetuals.

Reads COINALYZE_API_KEY from GitHub Actions Secrets. No trading endpoints.
Writes only public derived figures, source/symbol/time/quality metadata.
Separates OI (coin vs USD), estimated 5m futures CVD, aggregated reported
long/short liquidations and funding. Never fabricates unavailable data.
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api.coinalyze.net/v1"
ASSETS = ("BTC", "ETH")
OUT = Path("data/coinalyze_derivatives_latest.json")
UTC = timezone.utc
SEC = 60
WINDOWS = {"1h": 3600, "4h": 4 * 3600, "24h": 24 * 3600}


def iso(ts):
    """Convert Coinalyze timestamps (seconds or milliseconds) to UTC safely."""
    value = float(ts)
    # Coinalyze currently sends history timestamps in seconds, but some
    # current-data `update` fields are in milliseconds.
    if abs(value) >= 1e17:  # nanoseconds
        value /= 1_000_000_000
    elif abs(value) >= 1e14:  # microseconds
        value /= 1_000_000
    elif abs(value) >= 1e11:  # milliseconds
        value /= 1_000
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def finite_float(x):
    try:
        value = float(x)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def round_or_none(x, digits=3):
    return round(x, digits) if x is not None and math.isfinite(x) else None


def request(endpoint, params=None):
    key = os.environ.get("COINALYZE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Missing COINALYZE_API_KEY in GitHub Actions environment")
    url = f"{BASE}/{endpoint}"
    if params:
        url += "?" + urlencode(params)
    # Authentication lives in the request header, never in URLs or output JSON.
    for retry in range(3):
        try:
            req = Request(url, headers={"api_key": key, "User-Agent": "BTC-ETH-Market-Flow/1.0"})
            with urlopen(req, timeout=20) as response:
                obj = json.loads(response.read(5_000_000).decode("utf-8"))
            return obj
        except HTTPError as exc:
            if exc.code == 429 and retry < 2:
                after = finite_float(exc.headers.get("Retry-After"))
                time.sleep(max(1, min(after if after is not None else 2**retry, 15)))
                continue
            raise RuntimeError(f"{endpoint} HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            if retry < 2:
                time.sleep(1 + retry)
                continue
            raise RuntimeError(f"{endpoint} unavailable: {type(exc).__name__}") from None
    raise RuntimeError(f"{endpoint} unavailable")


def choose_binance_usdt_perps(exchanges, futures):
    """Discover exact venue instruments; never silently substitute other venues."""
    if not isinstance(exchanges, list) or not isinstance(futures, list):
        raise ValueError("unexpected metadata shape")
    binance_codes = {str(e.get("code", "")).casefold()
                     for e in exchanges if isinstance(e, dict)
                     and "binance" in str(e.get("name", "")).casefold()}
    binance_codes.discard("")
    result = {}
    for asset in ASSETS:
        candidates = []
        for f in futures:
            if not isinstance(f, dict):
                continue
            venue = str(f.get("exchange", "")).casefold()
            if not (venue in binance_codes or "binance" in venue):
                continue
            if str(f.get("base_asset", "")).upper() != asset:
                continue
            if str(f.get("quote_asset", "")).upper() != "USDT":
                continue
            if f.get("is_perpetual") is not True or str(f.get("margined", "")).upper() != "STABLE":
                continue
            # Binance futures usually identifies on-exchange BTCUSDT/ETHUSDT.
            if str(f.get("symbol_on_exchange", "")).upper() != asset + "USDT":
                continue
            if not f.get("symbol"):
                continue
            candidates.append(f)
        if len(candidates) == 1:
            result[asset] = candidates[0]
        elif len(candidates) > 1:
            # Prefer documented _PERP symbol; refuse if still ambiguous.
            refined = [f for f in candidates if "_PERP" in str(f["symbol"]).upper()]
            if len(refined) == 1:
                result[asset] = refined[0]
    return result


def rows_for(data, symbol):
    if not isinstance(data, list):
        raise ValueError("history API response not array")
    for item in data:
        if isinstance(item, dict) and item.get("symbol") == symbol:
            arr = item.get("history")
            if not isinstance(arr, list):
                raise ValueError("history is not an array")
            return sorted((x for x in arr if isinstance(x, dict) and isinstance(x.get("t"), int)),
                          key=lambda x: x["t"])
    return []


def latest_for(data, symbol):
    if not isinstance(data, list):
        raise ValueError("current endpoint response not array")
    return next((x for x in data if isinstance(x, dict) and x.get("symbol") == symbol), None)


def nearest_at(rows, target, tolerance_seconds, upper=None):
    choices = [r for r in rows if abs(r["t"] - target) <= tolerance_seconds
               and (upper is None or r["t"] <= upper)]
    return min(choices, key=lambda r: abs(r["t"] - target)) if choices else None


def summarize_oi(coin_rows, usd_rows, current_coin, current_usd, now):
    out = {"status": "unavailable", "units": "exchange-listed underlying denomination; converted series in USD",
           "changes": {}, "source": "Coinalyze Binance USDT perpetual open-interest-history"}
    # OI history c is point-in-time close, not volume or liquidation.
    if not coin_rows or not usd_rows:
        out["reason"] = "missing OI history"
        return out
    anchor = coin_rows[-1]["t"]
    anchor_usd = nearest_at(usd_rows, anchor, 15 * SEC)
    if not anchor_usd or now - anchor > 35 * SEC:
        out["reason"] = "OI history not sufficiently fresh/aligned"
        return out
    current_coin_value = finite_float(current_coin.get("value")) if isinstance(current_coin, dict) else None
    current_usd_value = finite_float(current_usd.get("value")) if isinstance(current_usd, dict) else None
    out.update({"status": "ok", "history_asof_utc": iso(anchor),
                "last_history_open_interest_native": finite_float(coin_rows[-1].get("c")),
                "last_history_open_interest_usd": finite_float(anchor_usd.get("c")),
                "current_native": current_coin_value, "current_usd": current_usd_value,
                "current_asof_utc": iso(current_coin.get("update")) if isinstance(current_coin, dict) and isinstance(current_coin.get("update"), int) else None})
    for window, secs in WINDOWS.items():
        prior_coin = nearest_at(coin_rows, anchor - secs, 18 * SEC, anchor)
        prior_usd = nearest_at(usd_rows, anchor - secs, 18 * SEC, anchor)
        if not prior_coin or not prior_usd:
            out["changes"][window] = {"status": "unavailable", "reason": "missing matched OI bars"}
            continue
        a, b = finite_float(coin_rows[-1].get("c")), finite_float(prior_coin.get("c"))
        au, bu = finite_float(anchor_usd.get("c")), finite_float(prior_usd.get("c"))
        if None in (a, b, au, bu):
            out["changes"][window] = {"status": "unavailable", "reason": "invalid numeric OI data"}
            continue
        out["changes"][window] = {"status": "ok", "start_utc": iso(prior_coin["t"]),
            "native_units_delta": round_or_none(a-b, 6),
            "native_units_pct": round_or_none(100*(a/b-1), 4) if b else None,
            "usd_notional_delta": round_or_none(au-bu, 2),
            "usd_notional_pct": round_or_none(100*(au/bu-1), 4) if bu else None,
            "note": "USD OI moves with price and position count; not all change is new leverage"}
    return out


# Duration of each official Coinalyze liquidation aggregation bin, seconds.
LIQ_INTERVALS = {"15min": 900, "1hour": 3600, "4hour": 14400, "6hour": 21600}
LIQ_PREFERENCES = {
    "1h": ("15min", "1hour"),
    "4h": ("15min", "1hour", "4hour"),
    "24h": ("15min", "1hour", "4hour", "6hour"),
}


def summarize_liquidations_multi(series_by_interval, now):
    """Calculate exact time-bucket totals from *complete, continuous* vendor bars.

    Coinalyze sometimes returns sparse 15min bars. Missing bars are *not*
    assumed zero. Request longer vendor-native aggregations instead. If a
    continuous set does not exist at any granularity, report unavailable.
    Distinct windows may end at distinct UTC bucket boundaries; preserve them.
    """
    out={"status":"unavailable", "unit":"USD", "windows":{},
         "source":"Coinalyze reported Binance long/short liquidation aggregates",
         "method":"Vendor-native 15min/1hour/4hour/6hour closed UTC bins; no zero filling",
         "limitations":"Time windows are bucket-aligned, not necessarily rolling to request time. Vendor figures may omit some liquidations."}
    for window, window_s in WINDOWS.items():
        best=None
        diagnostics=[]
        for interval in LIQ_PREFERENCES[window]:
            width=LIQ_INTERVALS[interval]
            if window_s % width: continue
            needed=window_s//width
            original=series_by_interval.get(interval, [])
            completed=[x for x in original if isinstance(x, dict) and isinstance(x.get("t"),int)
                       and x["t"]+width <= now - 60]
            if len(completed)<needed:
                diagnostics.append(f"{interval}: insufficient completed bars")
                continue
            group=completed[-needed:]
            if any(b["t"]-a["t"] != width for a,b in zip(group,group[1:])):
                diagnostics.append(f"{interval}: missing bins")
                continue
            values=[(finite_float(x.get("l")),finite_float(x.get("s"))) for x in group]
            if any(l is None or s is None or l<0 or s<0 for l,s in values):
                diagnostics.append(f"{interval}: invalid liquidation figure")
                continue
            end=group[-1]["t"]+width
            lag=now-end
            # A daily-style, coarse source may be several hours behind today.
            # Do not mistake this for a rolling latest-24h measure.
            if lag > max(40*60, width):
                diagnostics.append(f"{interval}: latest complete bin too old")
                continue
            longs=sum(v[0] for v in values)
            shorts=sum(v[1] for v in values)
            best={"status":"ok", "start_utc":iso(group[0]["t"]),
                  "end_utc":iso(end),"source_interval":interval,
                  "age_seconds_at_collection":lag,
                  "is_bucket_aligned":True,
                  "long_liquidations_usd":round_or_none(longs,2),
                  "short_liquidations_usd":round_or_none(shorts,2),
                  "total_liquidations_usd":round_or_none(longs+shorts,2)}
            break
        out["windows"][window]=best or {"status":"unavailable",
            "reason":"No complete contiguous vendor liquidation bins, without zero imputation",
            "checks":diagnostics}
    good=[v for v in out["windows"].values() if v["status"]=="ok"]
    if good:
        out["status"]="ok" if len(good)==len(WINDOWS) else "partial"
        out["last_complete_data_utc"]=max(v["end_utc"] for v in good)
    return out


def summarize_flow(rows, enabled, now):
    out={"status":"unavailable", "source":"Coinalyze Binance USDT perpetual OHLCV",
         "method":"5m volume v and buy volume bv; sell volume=v-bv; not raw tick reconstruction", "windows":{}}
    if not enabled:
        out["reason"]="market has_buy_sell_data is not true"
        return out
    if not rows:
        out["reason"]="no 5m OHLCV bars"
        return out
    # Exclude the currently forming bar, since Coinalyze timestamps start of interval.
    completed=[r for r in rows if r["t"] + 5*SEC <= now-SEC]
    if not completed or now-(completed[-1]["t"]+5*SEC)>20*SEC:
        out["reason"]="5m candles missing/stale"
        return out
    out["status"]="ok"
    out["last_complete_utc"]=iso(completed[-1]["t"]+5*SEC)
    for window, seconds in WINDOWS.items():
        n=seconds//(5*SEC)
        subset=completed[-n:]
        continuous=len(subset)==n and all(b["t"]-a["t"]==5*SEC for a,b in zip(subset,subset[1:]))
        if not continuous:
            out["windows"][window]={"status":"unavailable", "reason":"missing contiguous bars"}
            continue
        vols=[finite_float(r.get("v")) for r in subset]
        buys=[finite_float(r.get("bv")) for r in subset]
        if any(v is None or b is None or v<0 or b<0 or b>v*(1+1e-4) for v,b in zip(vols,buys)):
            out["windows"][window]={"status":"unavailable", "reason":"unsupported/missing buy-volume fields"}
            continue
        b=sum(buys); s=sum(v-u for v,u in zip(vols,buys))
        out["windows"][window]={"status":"ok", "start_utc":iso(subset[0]["t"]),
          "end_utc":iso(subset[-1]["t"]+5*SEC),
          "taker_buy_native_volume":round_or_none(b,6),
          "taker_sell_native_volume":round_or_none(s,6),
          "cvd_delta_native_volume":round_or_none(b-s,6),
          "taker_buy_share_pct":round_or_none(100*b/(b+s),3) if b+s else None,
          "quote_usd_delta":None, "note":"Native trade volume, not automatically USD"}
    return out


def get_current_utc(data, sym):
    x=latest_for(data,sym)
    if not isinstance(x,dict):return {"status":"unavailable"}
    val=finite_float(x.get("value")); ts=x.get("update")
    return {"status":"ok" if val is not None else "unavailable", "value":val,
            "updated_utc": iso(ts) if isinstance(ts,int) else None}


def main():
    if not os.environ.get("COINALYZE_API_KEY", "").strip():
        print("ERROR: COINALYZE_API_KEY missing (never print actual key)", file=sys.stderr)
        return 2
    now=int(time.time())
    output={"generated_utc":iso(now), "source":"Coinalyze public market data API",
        "docs":"https://api.coinalyze.net/v1/doc/", "note":"Venue-specific Binance USDT perpetuals. No cross-exchange aggregation.",
        "assets":{}, "quality_status":"unverified", "limitations":[
            "Coinalyze liquidation bars are reported aggregates, not a future liquidation heatmap. Coarse bins may be hours behind; inspect their end timestamps.",
            "OI changes do not identify which side initiated new positions.",
            "5m buy-sell volume is aggregate futures order flow, not trade-by-trade CVD.",
            "No order-book depth or exact absorption from Coinalyze API."]}
    try:
        exchanges=request("exchanges")
        futures=request("future-markets")
    except Exception as exc:
        print("ERROR: metadata unavailable: "+str(exc), file=sys.stderr)
        return 1
    markets=choose_binance_usdt_perps(exchanges,futures)
    for asset in ASSETS:
        if asset not in markets:
            output["assets"][asset]={"status":"unavailable", "reason":"Unambiguous Binance USDT perpetual market not identified"}
    syms=[markets[a]["symbol"] for a in ASSETS if a in markets]
    if not syms:
        print("ERROR: no matching Binance futures markets",file=sys.stderr)
        return 1
    # One endpoint call for up to two symbols (each symbol counts against API quota).
    symbol_param=",".join(syms)
    start=now-26*3600
    history_params={"symbols":symbol_param,"interval":"15min","from":start,"to":now}
    fetches={
      "current_oi_native":("open-interest",{"symbols":symbol_param}),
      "current_oi_usd":("open-interest",{"symbols":symbol_param,"convert_to_usd":"true"}),
      "current_funding":("funding-rate",{"symbols":symbol_param}),
      "oi_history_native":("open-interest-history",history_params),
      "oi_history_usd":("open-interest-history",{**history_params,"convert_to_usd":"true"}),
      **{f"liquidation_{interval}":("liquidation-history",
             {"symbols":symbol_param, "interval":interval,
              "from":now-42*3600, "to":now, "convert_to_usd":"true"})
         for interval in LIQ_INTERVALS},
      "five_min_ohlcv":("ohlcv-history",{"symbols":symbol_param,"interval":"5min","from":start,"to":now})
    }
    results={}; errors={}
    for label,(endpoint,params) in fetches.items():
        try: results[label]=request(endpoint,params)
        except Exception as exc:
            results[label]=[]
            errors[label]=str(exc)
    for asset in ASSETS:
        if asset not in markets:continue
        market=markets[asset]; sym=market["symbol"]
        item={"venue":"Binance (via Coinalyze)","symbol_coinalyze":sym,
              "symbol_on_exchange":market["symbol_on_exchange"],"margin_type":market["margined"],
              "oi_liquidation_volume_denomination":market.get("oi_lq_vol_denominated_in"),
              "quality":{},"errors":errors.copy()}
        try:
            native=rows_for(results["oi_history_native"],sym)
            usd=rows_for(results["oi_history_usd"],sym)
            item["open_interest"]=summarize_oi(native,usd,
              latest_for(results["current_oi_native"],sym),
              latest_for(results["current_oi_usd"],sym),now)
        except Exception as exc:
            item["open_interest"]={"status":"unavailable","reason":str(exc)}
        try:
            liquidations={interval:rows_for(results[f"liquidation_{interval}"],sym)
                          for interval in LIQ_INTERVALS}
            item["liquidations"]=summarize_liquidations_multi(liquidations,now)
        except Exception as exc:
            item["liquidations"]={"status":"unavailable","reason":str(exc)}
        try:
            flow=rows_for(results["five_min_ohlcv"],sym)
            item["futures_flow"]=summarize_flow(flow,market.get("has_buy_sell_data") is True,now)
        except Exception as exc:
            item["futures_flow"]={"status":"unavailable","reason":str(exc)}
        try:
            item["funding"]=get_current_utc(results["current_funding"],sym)
        except Exception as exc:
            item["funding"]={"status":"unavailable","reason":str(exc)}
        item["quality"]={f:item[f].get("status","unavailable") for f in ("open_interest","liquidations","futures_flow","funding")}
        output["assets"][asset]=item
    successful=sum(v=="ok" for a in output["assets"].values() for v in a.get("quality",{}).values())
    output["quality_status"]="partial" if successful else "no_usable_data"
    all_windows_ok=all(
        data.get("liquidations",{}).get("windows",{}).get(win,{}).get("status")=="ok"
        and data.get("open_interest",{}).get("changes",{}).get(win,{}).get("status")=="ok"
        and data.get("futures_flow",{}).get("windows",{}).get(win,{}).get("status")=="ok"
        for data in output["assets"].values() for win in WINDOWS)
    if successful==len(syms)*4 and all_windows_ok:
        output["quality_status"]="complete"
    else:
        output["quality_status"]="partial" if successful else "no_usable_data"
    if successful:
        OUT.parent.mkdir(parents=True,exist_ok=True)
        OUT.write_text(json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
        print("Saved",OUT,"status",output["quality_status"])
        for asset,data in output["assets"].items():print(asset,data.get("quality"))
        return 0
    print("ERROR: all market data unavailable; refusing false-success output",file=sys.stderr)
    for asset,datum in output["assets"].items():print(asset,datum.get("errors",{}),file=sys.stderr)
    return 1


if __name__=="__main__":
    sys.exit(main())
