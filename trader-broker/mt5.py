#!/usr/bin/env python3
"""MetaTrader 5 reader for the forex (FX) mode. READ ONLY: it never sends,
checks or changes an order. You place every FX order yourself in MT5.

It attaches to the MT5 terminal that is already running and logged in on this
computer (Windows), through MetaQuotes' official package:

    pip install MetaTrader5

It never asks for, reads or stores a login or password.

  export     5-minute bars -> trader/mt5/<PAIR>.json, in the same Yahoo chart
             shape trader-entry, trader-exit and trader-timemachine read with
             --feed / --offline, plus the pair's spec (digits, point, pip,
             contract size, lot limits, spread) under meta.mt5.
  follow     export again every --every seconds, for entry.py watch --feed.
  quote      bid / ask -> trader/prices.json (full precision).
  account    balance, equity, leverage, demo or real -> trader/mt5/account.json.
  positions  open positions and pending orders, to confirm what you placed.

    python3 mt5.py export EURUSD --days 60
    python3 mt5.py follow EURUSD --every 30
    python3 mt5.py quote EURUSD --write
    python3 mt5.py account --write
    python3 mt5.py positions

Times: MT5 stamps bars in the broker's SERVER time (Pepperstone: UTC+2 in
winter, UTC+3 in summer). The offset is measured from the latest tick against
this computer's clock, rounded to the hour, and every bar is shifted to true
UTC before it is written. When the market is shut (weekend) the last measured
offset is reused from trader/mt5/server.json, or pass --server-offset HOURS.
"""

import argparse
import datetime
import json
import os
import sys
import time

EPOCH = datetime.datetime(1970, 1, 1)
OUT_DIR = os.path.join("trader", "mt5")
FRESH_TICK = 600       # a tick older than this (seconds) cannot measure the server offset
M5 = 300


class MT5Error(Exception):
    pass


# ---------------------------------------------------------------- pure parts (tested)

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(ts):
    return (EPOCH + datetime.timedelta(seconds=int(ts))).strftime("%Y-%m-%dT%H:%M:%SZ")


def server_offset(tick_time, now_ts):
    """Hours the server clock runs ahead of UTC, from one fresh tick."""
    return int(round((int(tick_time) - int(now_ts)) / 3600.0))


def spec_of(info, tick=None):
    """MT5 symbol_info (+ tick) -> the spec every FX tool reads."""
    point = float(info["point"])
    digits = int(info["digits"])
    spec = {"digits": digits, "point": point, "pip": round(point * (10 if digits in (3, 5) else 1), 10),
            "contract_size": float(info["trade_contract_size"]), "volume_min": float(info["volume_min"]),
            "volume_step": float(info["volume_step"]), "volume_max": float(info["volume_max"]),
            "currency_base": info.get("currency_base"), "currency_profit": info.get("currency_profit"),
            "spread": None}
    if tick:
        spec["spread"] = round(float(tick["ask"]) - float(tick["bid"]), digits)
    return spec


def rates_to_chart(sym, rows, offset_hours, spec, tick=None, interval="5m"):
    """MT5 rates rows (time in server seconds, open, high, low, close, tick_volume)
    -> a Yahoo chart v8 document in true UTC."""
    shift = int(offset_hours) * 3600
    ts, o, h, l, c, v = [], [], [], [], [], []
    for r in rows:
        ts.append(int(r["time"]) - shift)
        o.append(float(r["open"]))
        h.append(float(r["high"]))
        l.append(float(r["low"]))
        c.append(float(r["close"]))
        v.append(int(r["tick_volume"]))
    last_ts = ts[-1] if ts else None
    meta = {"symbol": sym.upper(), "currency": spec.get("currency_profit"), "dataGranularity": interval,
            "exchangeName": "MT5", "instrumentType": "CURRENCY", "source": "mt5",
            "server_offset_hours": int(offset_hours), "mt5": spec}
    if tick:
        meta["regularMarketTime"] = int(tick["time"]) - shift
        meta["regularMarketPrice"] = float(tick["bid"])
    elif last_ts is not None:
        meta["regularMarketTime"] = last_ts + M5
        meta["regularMarketPrice"] = c[-1]
    return {"chart": {"result": [{"meta": meta, "timestamp": ts,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c,
                                                            "volume": v}]}}],
                      "error": None}}


def prices_doc(quotes, now_ts):
    """[{symbol, bid, ask, time, prev_close}] -> trader/prices.json (the alerts format)."""
    fetched = iso(now_ts)
    doc = {"as_of": fetched, "source": "mt5",
           "note": "MT5 bid prices for alerts and sizing checks. Your fill in MT5 is the price that counts.",
           "prices": {}, "detail": {}}
    for q in quotes:
        doc["prices"][q["symbol"]] = q["bid"]
        doc["detail"][q["symbol"]] = {"bid": q["bid"], "ask": q["ask"], "prev_close": q.get("prev_close"),
                                      "time": iso(q["time"]), "source": "mt5", "fetched": fetched}
    return doc


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    for attempt in range(20):
        try:
            os.replace(tmp, path)   # a reader polling the file never sees half of it
            return
        except PermissionError:     # Windows: a reader has it open this instant
            time.sleep(0.25)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- the terminal

def connect():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise MT5Error("the MetaTrader5 package is not installed: pip install MetaTrader5 (Windows only)")
    if not mt5.initialize():
        raise MT5Error("could not attach to MT5 (%s): open the terminal and log in first" % (mt5.last_error(),))
    return mt5


def _d(obj):
    return obj._asdict() if hasattr(obj, "_asdict") else dict(obj)


def symbol(mt5, sym):
    sym = sym.upper()
    info = mt5.symbol_info(sym)
    if info is None:
        near = [s.name for s in (mt5.symbols_get("*%s*" % sym) or [])][:8]
        raise MT5Error("MT5 has no symbol %s%s" % (sym, (": try %s" % ", ".join(near)) if near else ""))
    if not info.visible:
        mt5.symbol_select(sym, True)   # adds it to Market Watch so ticks flow; changes no order
    return _d(info)


def tick_of(mt5, sym):
    t = mt5.symbol_info_tick(sym)
    return _d(t) if t is not None else None


def measure_offset(mt5, sym, fixed=None, state_path=os.path.join(OUT_DIR, "server.json")):
    if fixed is not None:
        return int(fixed), "given"
    tick = tick_of(mt5, sym)
    now = ts_of(utc_now())
    saved = read_json(state_path, {}) or {}
    if tick and tick.get("time"):
        off = server_offset(tick["time"], now)
        if abs(int(tick["time"]) - off * 3600 - now) <= FRESH_TICK:
            write_json(state_path, {"server_offset_hours": off, "measured_at": iso(now), "symbol": sym})
            return off, "measured"
    if "server_offset_hours" in saved:
        return int(saved["server_offset_hours"]), "saved %s" % saved.get("measured_at", "")
    raise MT5Error("no fresh tick to measure the server's time offset (market shut?): pass --server-offset HOURS")


def export(mt5, sym, days, out, fixed_offset=None):
    info = symbol(mt5, sym)
    off, how = measure_offset(mt5, sym, fixed_offset)
    tick = tick_of(mt5, sym)
    count = int(days * 24 * 12) + 12
    rows = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, count)
    if rows is None or not len(rows):
        raise MT5Error("MT5 sent no 5-minute bars for %s (%s)" % (sym, mt5.last_error()))
    names = rows.dtype.names
    rows = [dict(zip(names, r)) for r in rows]
    doc = rates_to_chart(sym, rows, off, spec_of(info, tick), tick)
    write_json(out, doc)
    return doc, off, how, len(rows)


# ---------------------------------------------------------------- commands

def cmd_export(a, loop=False):
    mt5 = connect()
    try:
        out = a.out or os.path.join(OUT_DIR, a.symbol.upper() + ".json")
        while True:
            doc, off, how, n = export(mt5, a.symbol, a.days, out, a.server_offset)
            r = doc["chart"]["result"][0]
            m = r["meta"]
            print("%s  %d five-minute bars, %s to %s UTC (server UTC%+d, %s) -> %s" % (
                m["symbol"], n, iso(r["timestamp"][0])[:16].replace("T", " "),
                iso(r["timestamp"][-1])[:16].replace("T", " "), off, how, out), flush=True)
            if not loop:
                s = m["mt5"]
                print("spec: %d digits, pip %s, contract %s, lots %s to %s step %s, spread %s" % (
                    s["digits"], s["pip"], s["contract_size"], s["volume_min"], s["volume_max"],
                    s["volume_step"], s["spread"]))
                return 0
            time.sleep(max(5, a.every))
    finally:
        mt5.shutdown()


def cmd_quote(a):
    mt5 = connect()
    try:
        quotes = []
        for sym in a.symbols:
            info = symbol(mt5, sym)
            off, _ = measure_offset(mt5, sym, a.server_offset)
            t = tick_of(mt5, sym)
            if not t:
                raise MT5Error("no tick for %s" % sym)
            day = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_D1, 1, 1)
            prev = float(day[0]["close"]) if day is not None and len(day) else None
            d = int(info["digits"])
            q = {"symbol": sym.upper(), "bid": round(float(t["bid"]), d), "ask": round(float(t["ask"]), d),
                 "time": int(t["time"]) - off * 3600, "prev_close": prev}
            quotes.append(q)
            print("%-8s bid %.*f  ask %.*f  spread %.1f pips  %s UTC" % (
                q["symbol"], d, q["bid"], d, q["ask"],
                (q["ask"] - q["bid"]) / spec_of(info)["pip"], iso(q["time"])[11:19]))
        if a.write:
            write_json(a.write, prices_doc(quotes, ts_of(utc_now())))
            print("Wrote %s." % a.write)
        return 0
    finally:
        mt5.shutdown()


MODES = {0: "demo", 1: "contest", 2: "real"}


def cmd_account(a):
    mt5 = connect()
    try:
        acc = _d(mt5.account_info())
        doc = {"source": "mt5", "as_of": iso(ts_of(utc_now())), "mode": MODES.get(acc["trade_mode"], "unknown"),
               "server": acc["server"], "company": acc["company"], "currency": acc["currency"],
               "balance": acc["balance"], "equity": acc["equity"], "margin": acc["margin"],
               "margin_free": acc["margin_free"], "leverage": acc["leverage"]}
        print("%s account on %s: balance %s %.2f, equity %.2f, free margin %.2f, leverage 1:%d" % (
            doc["mode"].upper(), doc["server"], doc["currency"], doc["balance"], doc["equity"],
            doc["margin_free"], doc["leverage"]))
        if doc["mode"] == "real":
            print("This is a REAL money account. The skills still never place orders; you do.")
        if a.write:
            write_json(a.write, doc)
            print("Wrote %s." % a.write)
        return 0
    finally:
        mt5.shutdown()


def cmd_positions(a):
    mt5 = connect()
    try:
        pos = [_d(p) for p in (mt5.positions_get() or [])]
        orders = [_d(o) for o in (mt5.orders_get() or [])]
        if not pos and not orders:
            print("No open positions and no pending orders.")
        for p in pos:
            print("POSITION %s %s %.2f lots at %s, SL %s, TP %s, profit %.2f (ticket %s)" % (
                p["symbol"], "BUY" if p["type"] == 0 else "SELL", p["volume"], p["price_open"],
                p["sl"] or "none", p["tp"] or "none", p["profit"], p["ticket"]))
        for o in orders:
            print("PENDING  %s type %s %.2f lots at %s, SL %s, TP %s (ticket %s)" % (
                o["symbol"], o["type"], o["volume_current"], o["price_open"], o["sl"] or "none",
                o["tp"] or "none", o["ticket"]))
        if a.json:
            print(json.dumps({"positions": pos, "orders": orders}, indent=2, default=str))
        return 0
    finally:
        mt5.shutdown()


def main(argv=None):
    ap = argparse.ArgumentParser(description="MetaTrader 5 reader (read only, never places orders).")
    sub = ap.add_subparsers(dest="cmd")
    for name, helptext in (("export", "5-minute bars -> trader/mt5/<PAIR>.json"),
                           ("follow", "export again every --every seconds")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("symbol")
        p.add_argument("--days", type=float, default=60, help="how far back (default 60)")
        p.add_argument("--out", help="default trader/mt5/<PAIR>.json")
        p.add_argument("--server-offset", type=int, help="server hours ahead of UTC, if it cannot be measured")
        p.add_argument("--every", type=int, default=30)
    p = sub.add_parser("quote", help="bid / ask")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--write", nargs="?", const="trader/prices.json")
    p.add_argument("--server-offset", type=int)
    p = sub.add_parser("account", help="balance, equity, leverage")
    p.add_argument("--write", nargs="?", const=os.path.join(OUT_DIR, "account.json"))
    p = sub.add_parser("positions", help="open positions and pending orders")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd == "follow":
            return cmd_export(a, loop=True)
        return {"export": cmd_export, "quote": cmd_quote, "account": cmd_account,
                "positions": cmd_positions}[a.cmd](a)
    except MT5Error as e:
        print("mt5.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
