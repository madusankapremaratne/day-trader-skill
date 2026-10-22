"""Forex (FX) mode: EURUSD on MT5 data, end to end, without MT5 or the network.

The fixture is a real Pepperstone demo export (trader-broker's mt5.py export
EURUSD), 2026-09-07 to 2026-10-21, trimmed and gzipped; its spread is set to
0.2 pips so the spread rule has something to test."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

entry = load("trader-entry", "entry")
exitp = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
size = load("trader-budget", "size")
check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
mt5 = load("trader-broker", "mt5")
alerts = load("trader-alerts", "alerts")

FXDIR = os.path.join(FIX, "trader-fx")
FXFILE = os.path.join(FXDIR, "EURUSD.json.gz")
SPEC = {"digits": 5, "point": 0.00001, "pip": 0.0001, "contract_size": 100000.0, "volume_min": 0.01,
        "volume_step": 0.01, "volume_max": 100.0, "spread": 0.00002}
DAY = "2026-10-21"
NOW = "2026-10-21 03:21"
CFG = {"budget": 5000, "cash_buffer_pct": 10, "max_loss_per_trade_usd": 50, "max_daily_loss_usd": 150,
       "max_trades_per_day": 3, "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
       "allow_fx": True, "fx": {"pairs": ["EURUSD"], "max_lots": 1.0, "max_leverage_used": 10}}
ACCOUNT = {"mode": "demo", "balance": 50000.0, "leverage": 200, "currency": "USD"}


def fx_plan(**over):
    p = {"created_by": "trader-exit", "symbol": "EURUSD", "side": "buy", "date": DAY, "setup": "orb",
         "entry": 1.12228, "entry_time": "3:20 AM", "entry_bar": 3, "interval": "5m",
         "opening_range": {"high": 1.12214, "low": 1.12183, "minutes": 15}, "stop": 1.12198,
         "stop_rule": "or_mid", "target": 1.12288, "target_r": 2.0, "risk_per_share": 0.0003,
         "reward_risk": 2.0, "flat_by": "16:30", "market": "fx", "spec": dict(SPEC),
         "session": {"opens": "03:00", "closes": "16:30", "tz": "New York", "range_start": "08:00 London"}}
    p.update(over)
    return p


def fx_order(**over):
    o = {"market": "fx", "venue": "mt5", "symbol": "EURUSD", "side": "buy", "volume": "1.00", "type": "market",
         "sl": "1.12198", "tp": "1.12288",
         "plan": {"entry": 1.12228, "stop": 1.12198, "target": 1.12288, "spec": dict(SPEC)}}
    o.update(over)
    return o


def quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(*args)
    return code, out.getvalue(), err.getvalue()


class Clock(unittest.TestCase):
    def test_london_summer_time_edges(self):
        u = datetime.datetime
        self.assertEqual(entry.london_offset(u(2026, 3, 29, 0, 59)), 0)
        self.assertEqual(entry.london_offset(u(2026, 3, 29, 1, 0)), 1)
        self.assertEqual(entry.london_offset(u(2026, 10, 25, 0, 59)), 1)
        self.assertEqual(entry.london_offset(u(2026, 10, 25, 1, 0)), 0)

    def test_the_london_open_in_new_york_time_moves_in_the_mismatch_weeks(self):
        fx = entry.fx_profile({}, "EURUSD")
        for day, opens in (("2026-10-21", "03:00"), ("2026-10-26", "04:00"), ("2026-11-02", "03:00"),
                           ("2026-03-09", "04:00"), ("2026-03-30", "03:00")):
            o, c, cut = entry.fx_session(datetime.date.fromisoformat(day), fx)
            self.assertEqual((o.strftime("%H:%M"), c.strftime("%H:%M"), cut.strftime("%H:%M")),
                             (opens, "16:30", "15:30"), day)
        self.assertIsNone(entry.fx_session(datetime.date(2026, 10, 24), fx))

    def test_the_same_rule_in_every_tool(self):
        fx = dict(entry.FX_DEFAULTS)
        for day in ("2026-10-21", "2026-10-26", "2026-03-09"):
            d = datetime.date.fromisoformat(day)
            o = entry.fx_session(d, fx)[0]
            self.assertEqual(exitp.fx_session(d, dict(exitp.FX_DEFAULTS))[0], o, day)
            self.assertEqual(check.wall_to_et(d, datetime.time(8, 0), "London"), o, day)
            self.assertEqual(tm.Geo({"or_start": 480, "or_tz": "London", "close": 990}).open_min(d),
                             o.hour * 60 + o.minute, day)

    def test_fx_mode_is_for_listed_usd_pairs(self):
        self.assertIsNone(entry.fx_profile({}, "TSLA"))
        self.assertIsNotNone(entry.fx_profile({}, "EURUSD"))
        with self.assertRaises(entry.EntryError):
            entry.fx_profile({"fx": {"pairs": ["EURJPY"]}}, "EURJPY")


class Entry(unittest.TestCase):
    def setUp(self):
        self.chart = entry.get_chart("EURUSD", "5m", offline=FXDIR)
        self.fx = entry.fx_profile({}, "EURUSD")

    def test_the_london_open_range_and_the_trigger(self):
        r = entry.evaluate(self.chart, entry.parse_now(NOW), or_minutes=None, fx=self.fx)
        self.assertEqual(r["status"], "triggered")
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"]), (1.12214, 1.12183))
        self.assertEqual((r["trigger"]["price"], r["trigger"]["time"], r["trigger"]["bar_index"]),
                         (1.12228, "3:20 AM", 3))
        lines = "\n".join(entry.describe(r))
        self.assertIn("1.12228", lines)
        self.assertIn("the 08:00 London open", lines)
        self.assertNotIn("$1.12", lines)
        doc = entry.entry_doc(r, entry.parse_now(NOW))
        self.assertEqual((doc["market"], doc["source"], doc["spec"]["digits"]), ("fx", "mt5", 5))
        self.assertEqual(doc["session"]["opens"], "03:00")

    def test_before_the_london_open(self):
        r = entry.evaluate(self.chart, entry.parse_now("2026-10-21 02:40"), or_minutes=None, fx=self.fx)
        self.assertEqual(r["phase"], "pre-open")
        self.assertIn("08:00 London", r["reason"])

    def test_the_weekend(self):
        r = entry.evaluate(self.chart, entry.parse_now("2026-10-24 09:00"), or_minutes=None, fx=self.fx)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))

    def test_check_with_a_feed_writes_an_fx_entry(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "entry.json")
            code, text, err = quiet(entry.main, ["check", "EURUSD", "--offline", FXDIR, "--now", NOW,
                                                 "--config", os.path.join(d, "none.json"), "--write", out])
            self.assertEqual(code, 0, err)
            with open(out) as fh:
                self.assertEqual(json.load(fh)["market"], "fx")
        finally:
            shutil.rmtree(d)


class Exit(unittest.TestCase):
    def fx_math(self, **over):
        m = dict(SPEC, min_stop_spreads=3, min_stop_pips=3)
        m.update(over)
        return m

    def test_stops_round_down_to_the_point(self):
        c = exitp.compute(1.12228, "or_mid", None, "r", exitp.D(2), 1.12214, 1.12183, fx=self.fx_math())
        self.assertEqual((float(c["stop"]), float(c["target"]), float(c["risk"])), (1.12198, 1.12288, 0.0003))
        self.assertIn("1.121985, rounded down", c["stop_detail"])

    def test_too_tight_by_pips_and_by_spread(self):
        with self.assertRaises(exitp.PlanError) as e:
            exitp.compute(1.12228, "price", exitp.D("1.12210"), "r", exitp.D(2), fx=self.fx_math())
        self.assertIn("1.8 pips", str(e.exception))
        with self.assertRaises(exitp.PlanError) as e:
            exitp.compute(1.12228, "or_mid", None, "r", exitp.D(2), 1.12214, 1.12183,
                          fx=self.fx_math(spread=0.0002, min_stop_pips=0))
        self.assertIn("6-pip minimum", str(e.exception))

    def test_a_plan_from_the_entry(self):
        d = tempfile.mkdtemp()
        try:
            e = os.path.join(d, "entry.json")
            p = os.path.join(d, "plan.json")
            cfg = os.path.join(d, "config.json")
            with open(cfg, "w") as fh:
                json.dump({}, fh)
            quiet(entry.main, ["check", "EURUSD", "--offline", FXDIR, "--now", NOW, "--config", cfg, "--write", e])
            code, text, err = quiet(exitp.main, ["plan", "--from", e, "--feed", FXDIR, "--now", NOW,
                                                 "--config", cfg, "--write", p])
            self.assertEqual(code, 0, err)
            self.assertIn("3 pips", text)
            self.assertIn("16:30 ET", text)
            with open(p) as fh:
                plan = json.load(fh)
            self.assertEqual((plan["market"], plan["stop"], plan["target"], plan["flat_by"], plan["risk_pips"]),
                             ("fx", 1.12198, 1.12288, "16:30", 3.0))
            self.assertEqual(plan["session"]["range_start"], "08:00 London")
        finally:
            shutil.rmtree(d)


class TimeMachine(unittest.TestCase):
    def test_replays_on_the_mt5_bars(self):
        with open(FXFILE, "rb") as fh:
            import gzip
            text = gzip.decompress(fh.read()).decode("utf-8")
        plan = tm.normalize_plan(fx_plan())
        self.assertEqual((plan["entry_bar"], plan["entry_time"]), (3, "3:20 AM"))
        doc = tm.time_machine(text, plan, tm.parse_now(NOW), replays=2000)
        m = doc["mc"]
        self.assertGreaterEqual(m["pool_sessions"], 20)
        self.assertEqual(m["until"], "16:30")
        self.assertEqual(m["digits"], 5)
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=3)
        self.assertGreater(doc["history"]["triggered"], 0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_mid")
        text = "\n".join(tm.report(doc))
        self.assertIn("3.0 pips", text)
        self.assertIn("1.12228", text)

    def test_stock_shape_is_unchanged(self):
        self.assertEqual((tm.STOCKS.open_min(datetime.date(2026, 10, 19)), tm.STOCKS.bars(None)), (570, 78))


class Budget(unittest.TestCase):
    def test_lots_to_the_max_loss(self):
        s = size.size_fx(fx_plan(stop=1.12148, target=1.12388), CFG, ACCOUNT)
        self.assertEqual((s["lots"], s["bound_by"], s["per_lot"], s["max_loss"]), (0.62, "max loss", 80.0, 49.6))
        o = size.fx_order(s)
        self.assertEqual((o["volume"], o["sl"], o["tp"], o["venue"]), ("0.62", "1.12148", "1.12388", "mt5"))

    def test_each_limit_can_decide(self):
        p = fx_plan(stop=1.12148, target=1.12388)
        cfg = dict(CFG, fx=dict(CFG["fx"], max_lots=0.3))
        self.assertEqual(size.size_fx(p, cfg, ACCOUNT)["bound_by"], "max lots")
        small = dict(ACCOUNT, balance=1000.0, leverage=30)
        s = size.size_fx(p, dict(CFG, fx=dict(CFG["fx"], max_leverage_used=100)), small)
        self.assertEqual((s["bound_by"], s["lots"]), ("margin", 0.24))
        cfg = dict(CFG, fx=dict(CFG["fx"], max_leverage_used=1))
        self.assertEqual(size.size_fx(p, cfg, ACCOUNT)["bound_by"], "leverage")

    def test_a_stop_too_wide_for_the_smallest_lot(self):
        s = size.size_fx(fx_plan(stop=1.11228, target=1.14228), dict(CFG, max_loss_per_trade_usd=5), ACCOUNT)
        self.assertEqual(s["lots"], 0)
        self.assertIsNone(size.fx_order(s))
        self.assertIn("0.01 lots", s["refusal"])

    def test_a_stale_plan_is_refused(self):
        with self.assertRaises(size.SizeError):
            size.size_fx(fx_plan(), CFG, ACCOUNT, today="2026-10-22")


class Risk(unittest.TestCase):
    def run_check(self, orders, now=NOW, cfg=CFG, state=None):
        return check.check(orders, cfg, None, {}, state, None, check.parse_now(now), None, ACCOUNT)

    def test_off_by_default(self):
        r = self.run_check([fx_order()], cfg=dict(CFG, allow_fx=False))
        self.assertEqual(r["block"], 1)
        self.assertIn("allow_fx", r["results"][0]["reasons"][0])

    def test_passes_inside_the_window(self):
        r = self.run_check([fx_order()])
        self.assertEqual(r["pass"], 1, r["results"][0]["reasons"])
        self.assertEqual(r["results"][0]["max_loss"], 30.0)
        self.assertTrue(r["fx_only"])
        self.assertIn("MT5 DEMO account", check.report(r))

    def test_the_window(self):
        for now, word in (("2026-10-21 03:10", "before the opening range"), ("2026-10-21 15:45", "after 15:30"),
                          ("2026-10-24 09:00", "weekend")):
            r = self.run_check([fx_order()], now=now)
            self.assertEqual(r["block"], 1, now)
            self.assertTrue(any(word in x for x in r["results"][0]["reasons"]), r["results"][0]["reasons"])

    def test_bad_fx_orders(self):
        cases = [(fx_order(volume="0.015"), "multiple of"), (fx_order(volume="2.00"), "max_lots"),
                 (fx_order(volume="1.80"), "max loss"), (fx_order(sl=None), "stop"),
                 (fx_order(sl="1.12300"), "below the entry"), (fx_order(tp="1.12240"), "reward:risk"),
                 (fx_order(side="sell"), "longs only"), (fx_order(venue="alpaca"), "MT5"),
                 (fx_order(symbol="EURJPY"), "fx.pairs")]
        cfg = dict(CFG, fx=dict(CFG["fx"], max_lots=2.0))
        for o, word in cases[:1] + cases[2:]:
            r = self.run_check([o], cfg=cfg)
            self.assertEqual(r["block"], 1, o)
            self.assertTrue(any(word in x for x in r["results"][0]["reasons"]), (word, r["results"][0]["reasons"]))
        r = self.run_check([cases[1][0]])
        self.assertTrue(any("max_lots" in x for x in r["results"][0]["reasons"]))

    def test_leverage(self):
        cfg = dict(CFG, max_loss_per_trade_usd=500, fx=dict(CFG["fx"], max_lots=10, max_leverage_used=1))
        r = self.run_check([fx_order()], cfg=cfg)
        self.assertTrue(any("leverage" in x for x in r["results"][0]["reasons"]))

    def test_a_close_is_never_blocked_by_the_window(self):
        o = {"market": "fx", "venue": "mt5", "symbol": "EURUSD", "side": "sell", "volume": "1.00",
             "type": "market", "close": True}
        r = self.run_check([o], now="2026-10-21 16:20")
        self.assertEqual((r["pass"], r["results"][0]["kind"]), (1, "exit"))

    def test_the_daily_loss_counts_fx(self):
        state = {"orders": [{"date": DAY, "status": "placed", "symbol": "EURUSD", "side": "buy", "kind": "entry",
                             "max_loss": 130}]}
        r = self.run_check([fx_order(symbol="EURUSD")], state=state)
        self.assertTrue(any("daily loss" in x for x in r["results"][0]["reasons"]))


class Broker(unittest.TestCase):
    def test_the_ticket_is_typed_into_mt5_by_hand(self):
        risk = {"date": DAY, "results": [{"fingerprint": ticket.fingerprint(fx_order()), "verdict": "PASS",
                                          "kind": "entry", "warnings": []}]}
        text, ready = ticket.render(fx_order(), 1, 1, {}, risk, {"orders": []}, DAY)
        self.assertTrue(ready)
        self.assertIn("New Order (F9)", text)
        self.assertIn("Volume 1.00", text)
        self.assertIn("Stop Loss 1.12198, Take Profit 1.12288", text)
        self.assertIn("max loss about $30", text)
        self.assertNotIn("place_stock_order", text)
        tj = ticket.ticket_json(fx_order(), 1, {}, risk, {"orders": []}, DAY)
        self.assertIsNone(tj["tool"])

    def test_fingerprints_see_the_lots_and_match_trader_risk(self):
        self.assertNotEqual(ticket.fingerprint(fx_order()), ticket.fingerprint(fx_order(volume="2.00")))
        self.assertEqual(ticket.fingerprint(fx_order()), check.fingerprint(fx_order()))

    def test_a_yes_must_match_the_lots(self):
        self.assertFalse(ticket.parse_yes("yes 2 lots", fx_order())[0])
        self.assertTrue(ticket.parse_yes("yes 1.00 lots", fx_order())[0])

    def test_flatten_writes_a_close_ticket(self):
        d = tempfile.mkdtemp()
        try:
            orders = os.path.join(d, "orders.json")
            code, text, err = quiet(ticket.main, ["flatten", "--symbol", "EURUSD", "--qty", "1", "--orders", orders,
                                                  "--config", os.path.join(d, "none.json"),
                                                  "--plan", os.path.join(d, "none.json"), "--now", "2026-10-21 16:25"])
            self.assertEqual(code, 0, err)
            with open(orders) as fh:
                o = json.load(fh)["orders"][0]
            self.assertEqual((o["market"], o["close"], o["volume"]), ("fx", True, "1.00"))
            self.assertIn("Close Position", text)
        finally:
            shutil.rmtree(d)


class MT5Reader(unittest.TestCase):
    def test_server_time_becomes_utc(self):
        rows = [{"time": 1761134400 + 3 * 3600, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15,
                 "tick_volume": 7}]
        doc = mt5.rates_to_chart("eurusd", rows, 3, mt5.spec_of({"point": 1e-5, "digits": 5,
                                                               "trade_contract_size": 100000.0, "volume_min": 0.01,
                                                               "volume_step": 0.01, "volume_max": 100.0}))
        r = doc["chart"]["result"][0]
        self.assertEqual(r["timestamp"], [1761134400])
        self.assertEqual((r["meta"]["symbol"], r["meta"]["dataGranularity"], r["meta"]["source"]),
                         ("EURUSD", "5m", "mt5"))
        self.assertEqual(r["meta"]["mt5"]["pip"], 0.0001)
        self.assertEqual(entry.parse_chart(json.dumps(doc), "EURUSD")["bars"][0]["c"], 1.15)

    def test_offset_from_a_tick(self):
        self.assertEqual(mt5.server_offset(1000 + 3 * 3600 + 40, 1000), 3)
        self.assertEqual(mt5.server_offset(1000 + 2 * 3600 - 50, 1000), 2)

    def test_prices_file_feeds_the_alerts(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "prices.json")
            now = datetime.datetime(2026, 10, 22, 11, 0, 0)
            mt5.write_json(path, mt5.prices_doc([{"symbol": "EURUSD", "bid": 1.11991, "ask": 1.11993,
                                                  "time": mt5.ts_of(now) - 30, "prev_close": 1.1235}],
                                                mt5.ts_of(now)))
            q, errors = alerts.get_quotes(["EURUSD"], "mt5", mt5_path=path, now_utc=now)
            self.assertEqual((q["EURUSD"]["price"], errors), (1.11991, []))
            q, errors = alerts.get_quotes(["EURUSD"], "mt5", mt5_path=path,
                                          now_utc=now + datetime.timedelta(minutes=10))
            self.assertEqual(q, {})
            self.assertIn("over 180 seconds old", errors[0])
        finally:
            shutil.rmtree(d)


class Safety(unittest.TestCase):
    def test_nothing_sends_an_mt5_order(self):
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", "tests")]
            for f in files:
                if f.endswith((".py", ".md")):
                    with open(os.path.join(base, f), encoding="utf-8") as fh:
                        src = fh.read()
                    for word in ("order_send", "order_check"):
                        self.assertNotIn(word, src, os.path.join(base, f))


if __name__ == "__main__":
    unittest.main()
