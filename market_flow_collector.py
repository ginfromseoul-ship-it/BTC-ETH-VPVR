#!/usr/bin/env python3
"""Read-only Binance public BTC/ETH spot vs USDT-M futures market structure.

Records closed 5m taker-flow bars, 1h/4h/24h CVD proxies, OI history,
funding, and ±0.1/0.5/1% order book snapshots. Not a liquidation tape,
not true tick-level CVD, and not proof of order-book absorption.
"""
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

UTC = timezone.utc
SYMBOLS = ("BTCUSDT", "ETHUSDT")
SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"
OUT = Path("data/market_flow_latest.json")
HIST = Path("data/market_flow_history.json")
BAR_MS = 5 * 60 * 1000
WINDOWS = {"1h": 12, "4h": 48, "24h": 288}
LEVELS = (0.001, 0.005, 0.01)


def iso_ms(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def get_json(base, path, params, timeout=17):
    url = f"{base}{path}?{urlencode(params)}"
    last = None
    for n in range(2):
        try:
            req = Request(url, headers={"User-Agent": "BTC-ETH-Public-Market-Structure/1.0"})
            with urlopen(req, timeout=timeout) as response:
                raw = response.read(1_500_000)
            return json.loads(raw)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            code = getattr(exc, "code", None)
            last = f"{type(exc).__name__}" + (f" HTTP {code}" if code else "")
            if code in (400, 401, 403, 404, 418, 451):
                break
            if n == 0:
                time.sleep(1)
    raise RuntimeError(f"{path}: {last}; endpoint unavailable (not substituted)")


def summarize_klines(rows, now_ms):
    bars = []
    for r in rows:
        try:
            ts = int(r[0]); close_ts = int(r[6]); vol = float(r[5]); quote = float(r[7]); taker_buy = float(r[9]); taker_buy_quote = float(r[10])
            if ts + BAR_MS > now_ms - 2000:  # omit in-progress candles
                continue
            if not all(math.isfinite(z) for z in (vol, quote, taker_buy, taker_buy_quote)):
                continue
            if any(z < 0 for z in (vol, quote, taker_buy, taker_buy_quote)) or taker_buy > vol * (1 + 1e-5) or taker_buy_quote > quote * (1 + 1e-5):
                continue
            bars.append({"start_ms": ts, "end_ms": close_ts, "open": float(r[1]), "close": float(r[4]),
                         "volume_base": vol, "taker_buy_base": taker_buy,
                         "taker_sell_base": max(0.0, vol - taker_buy),
                         "volume_quote_usdt": quote, "taker_buy_quote_usdt": taker_buy_quote,
                         "taker_sell_quote_usdt": max(0.0, quote - taker_buy_quote),
                         "delta_quote_usdt": 2 * taker_buy_quote - quote})
        except (ValueError, TypeError, IndexError):
            continue
    bars = sorted({b["start_ms"]: b for b in bars}.values(), key=lambda b:b["start_ms"])
    if not bars:
        raise ValueError("no valid finished 5-minute bars")
    latest_end = bars[-1]["start_ms"] + BAR_MS
    if now_ms - latest_end > 15 * 60 * 1000:
        raise ValueError("latest closed candle is over 15 minutes stale")
    out = {"last_closed_bar_utc": iso_ms(latest_end), "latest_close_usdt": bars[-1]["close"], "windows":{}, "last_5m_bars":[]}
    for key, n in WINDOWS.items():
        chosen = bars[-n:]
        continuous = len(chosen) == n and all(b["start_ms"] - a["start_ms"] == BAR_MS for a,b in zip(chosen,chosen[1:]))
        if not continuous:
            out["windows"][key] = {"status": "unavailable", "reason": "missing contiguous closed bars"}
            continue
        buy_quote = sum(b["taker_buy_quote_usdt"] for b in chosen)
        sell_quote = sum(b["taker_sell_quote_usdt"] for b in chosen)
        buy_base = sum(b["taker_buy_base"] for b in chosen)
        sell_base = sum(b["taker_sell_base"] for b in chosen)
        total = buy_quote + sell_quote
        out["windows"][key] = {"status":"ok", "taker_buy_quote_usdt":round(buy_quote,2),
            "taker_sell_quote_usdt":round(sell_quote,2),
            "cvd_delta_quote_usdt":round(buy_quote-sell_quote,2),
            "taker_buy_base":round(buy_base,6), "taker_sell_base":round(sell_base,6),
            "cvd_delta_base":round(buy_base-sell_base,6),
            "buy_share_quote_pct":round(100*buy_quote/total,3) if total else None,
            "price_change_pct":round(100*(chosen[-1]["close"]/chosen[0]["open"]-1),4) if chosen[0]["open"] else None,
            "n_bars":n, "start_utc":iso_ms(chosen[0]["start_ms"]), "end_utc":iso_ms(latest_end)}
    for b in bars[-48:]:
        out["last_5m_bars"].append({"start_utc":iso_ms(b["start_ms"]),"close":b["close"],
               "taker_buy_quote_usdt":round(b["taker_buy_quote_usdt"],2),
               "taker_sell_quote_usdt":round(b["taker_sell_quote_usdt"],2),
               "cvd_delta_quote_usdt":round(b["delta_quote_usdt"],2)})
    return out


def summarize_depth(book, now_ms):
    bids = sorted(((float(p),float(q)) for p,q in book["bids"] if float(p)>0 and float(q)>=0),reverse=True)
    asks = sorted(((float(p),float(q)) for p,q in book["asks"] if float(p)>0 and float(q)>=0))
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        raise ValueError("missing or crossed order book")
    mid = (bids[0][0] + asks[0][0])/2
    if "T" in book:
        event_ms = int(book["T"])
    elif "E" in book:
        event_ms = int(book["E"])
    else:
        event_ms = now_ms   # Spot REST snapshot has no exchange event timestamp.
    if now_ms-event_ms > 60_000:
        raise ValueError("depth response too stale")
    out = {"observed_utc":iso_ms(now_ms), "exchange_event_time_utc":iso_ms(event_ms) if ("T" in book or "E" in book) else None,
           "mid_usdt": round(mid,6), "best_bid":bids[0][0], "best_ask":asks[0][0],
           "spread_bps":round((asks[0][0]-bids[0][0])/mid*10000,4),"levels_returned_bids":len(bids),"levels_returned_asks":len(asks),"depth_bands":{}}
    for pct in LEVELS:
        bid_floor=mid*(1-pct); ask_ceiling=mid*(1+pct)
        complete_bid=bids[-1][0] <= bid_floor
        complete_ask=asks[-1][0] >= ask_ceiling
        bn = sum(p*q for p,q in bids if p>=bid_floor)
        an = sum(p*q for p,q in asks if p<=ask_ceiling)
        out["depth_bands"][f"plus_minus_{pct*100:g}pct"]={"bid_notional_usdt":round(bn,2) if complete_bid else None,
            "ask_notional_usdt":round(an,2) if complete_ask else None,
            "coverage_complete_bid":complete_bid,"coverage_complete_ask":complete_ask,
            "bid_ask_ratio":round(bn/an,4) if complete_bid and complete_ask and an>0 else None}
    return out


def summarize_oi(current, history):
    out={"status":"ok", "current_contract_units":float(current["openInterest"]),"current_time_utc":iso_ms(int(current["time"])),"changes":{}}
    vals = sorted(((int(row["timestamp"]),float(row["sumOpenInterest"]),float(row.get("sumOpenInterestValue",0))) for row in history),key=lambda a:a[0])
    if not vals:
        out["status"]="partial"; return out
    if int(time.time()*1000)-vals[-1][0] > 2*3600*1000:
        raise ValueError("OI history older than 2 hours")
    out["latest_history_utc"]=iso_ms(vals[-1][0]); out["latest_history_contract_units"]=vals[-1][1]
    out["latest_history_notional_usdt"]=round(vals[-1][2],2) if vals[-1][2]>0 else None
    for n in (1,4,24):
        target=vals[-1][0]-n*3600*1000
        choices=[v for v in vals if abs(v[0]-target)<=35*60*1000]
        if not choices:
            out["changes"][f"{n}h"]={"status":"unavailable"}
            continue
        before=min(choices,key=lambda z:abs(z[0]-target))
        diff=vals[-1][1]-before[1]
        change={"status":"ok", "open_interest_contract_units_change":round(diff,6),
                "open_interest_contract_units_pct":round(100*diff/before[1],4) if before[1] else None}
        if vals[-1][2]>0 and before[2]>0:
            change["notional_change_usdt"]=round(vals[-1][2]-before[2],2)
            change["notional_change_pct"]=round(100*(vals[-1][2]/before[2]-1),4)
        out["changes"][f"{n}h"]=change
    return out


def collect_symbol(sym, now_ms):
    report={"symbol":sym,"spot":{},"futures":{},"quality":{},"limitations":[
      "Kline taker-buy vs remainder is a 5m aggregated trade-direction proxy; no trade-level reconstruction.",
      "Order-book depth is a momentary snapshot, not executed absorption or historical cancellations.",
      "Reported liquidation totals: unavailable; cannot infer liquidations from OI alone.",
      "Spot/futures causality or precise leader: not determined automatically."]}
    for market,base,kline_path,depth_path in (
      ("spot",SPOT,"/api/v3/klines","/api/v3/depth"),
      ("futures",FUTURES,"/fapi/v1/klines","/fapi/v1/depth")):
        for label,path,params,transform in (
          ("flow",kline_path,{"symbol":sym,"interval":"5m","limit":300},lambda x:summarize_klines(x,now_ms)),
          ("depth",depth_path,{"symbol":sym,"limit":1000},lambda x:summarize_depth(x,now_ms))):
            try:
                report[market][label]=transform(get_json(base,path,params))
                report["quality"][f"{market}_{label}"]="ok"
            except Exception as exc:
                report[market][label]={"status":"unavailable","reason":str(exc)}
                report["quality"][f"{market}_{label}"]="unavailable"
    try:
        oi=get_json(FUTURES,"/fapi/v1/openInterest",{"symbol":sym})
        hist=get_json(FUTURES,"/futures/data/openInterestHist",{"symbol":sym,"period":"1h","limit":30})
        report["futures"]["open_interest"]=summarize_oi(oi,hist)
        report["quality"]["futures_oi"]="ok"
    except Exception as exc:
        report["futures"]["open_interest"]={"status":"unavailable","reason":str(exc)}
        report["quality"]["futures_oi"]="unavailable"
    try:
        idx=get_json(FUTURES,"/fapi/v1/premiumIndex",{"symbol":sym})
        report["futures"]["funding"]={"last_funding_rate":float(idx["lastFundingRate"]),
            "next_funding_time_utc":iso_ms(int(idx["nextFundingTime"])),
            "mark_price":float(idx["markPrice"]),"index_price":float(idx["indexPrice"]),
            "funding_basis_bps":round(10000*(float(idx["markPrice"])/float(idx["indexPrice"])-1),4)}
        report["quality"]["futures_funding"]="ok"
    except Exception as exc:
        report["futures"]["funding"]={"status":"unavailable","reason":str(exc)}
        report["quality"]["futures_funding"]="unavailable"
    return report


def history_entry(full):
    compact={"collected_utc":full["collected_utc"],"assets":{}}
    for sym, d in full["assets"].items():
        compact["assets"][sym]={}
        for market in ("spot","futures"):
            m=d[market]; f=m.get("flow",{}); dep=m.get("depth",{});
            compact["assets"][sym][market]={"mid_usdt":dep.get("mid_usdt"),
                "spread_bps":dep.get("spread_bps"),
                "depth_bands":dep.get("depth_bands"),
                "cvd_delta_quote_usdt_1h":f.get("windows",{}).get("1h",{}).get("cvd_delta_quote_usdt"),
                "cvd_delta_quote_usdt_4h":f.get("windows",{}).get("4h",{}).get("cvd_delta_quote_usdt"),
                "cvd_delta_quote_usdt_24h":f.get("windows",{}).get("24h",{}).get("cvd_delta_quote_usdt")}
        oi=d["futures"].get("open_interest",{})
        compact["assets"][sym]["futures"]["oi_contract_units"]=oi.get("current_contract_units")
    return compact


def main():
    now_ms=int(time.time()*1000)
    full={"collected_utc":iso_ms(now_ms),"source":"Binance public spot market-data and USD-M futures REST endpoints",
        "market_scope":"Binance BTCUSDT ETHUSDT spot and USDT-margined perpetual futures (separate)",
        "cadence":"approx 15min GitHub Actions scheduler; not real-time or continuous surveillance",
        "assets":{},"quality_status":"unverified"}
    for sym in SYMBOLS:
        full["assets"][sym]=collect_symbol(sym,now_ms)
    core=[full["assets"][sym]["quality"].get(f"{market}_flow") == "ok" for sym in SYMBOLS for market in ("spot","futures")]
    full["quality_status"]="complete_core" if all(core) else ("partial_core" if any(core) else "no_core_data")
    full["limitations"]=["No exact order-flow absorption conclusion can be drawn from intermittent depth snapshots.",
      "No public complete retrospective liquidation tape provided by this script; no invented liquidation totals.",
      "Order book USD-depth reports only when the requested range fits within returned 1000 levels.",
      "OI open contracts vs OI notional must be interpreted separately."]
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(full,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    prior=[]
    if HIST.exists():
        try:
            stored=json.loads(HIST.read_text(encoding="utf-8"))
            if isinstance(stored,list):prior=stored
        except (ValueError,OSError):
            pass
    cutoff=now_ms-72*3600*1000
    updated=[p for p in prior if isinstance(p,dict) and p.get("collected_utc") and datetime.fromisoformat(p["collected_utc"].replace('Z','+00:00')).timestamp()*1000>=cutoff]
    updated.append(history_entry(full))
    updated=updated[-300:]
    HIST.write_text(json.dumps(updated,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    print(f"Saved {OUT} and {HIST}. Core data status: {full['quality_status']}",flush=True)
    for sym in SYMBOLS:
        print(sym,full["assets"][sym]["quality"],flush=True)
    # No normal-looking success when none of the core market feeds worked.
    return 1 if full["quality_status"]=="no_core_data" else 0

if __name__ == "__main__":
    sys.exit(main())
