"""Tests for the GenZ price engine (src/genz/engine.py).

Covers: a 2-outcome market whose best-of-both sums < 1 is flagged an arb; 3-way moneyline nodes are
skipped; walk-to-stake (not top-of-book) sets the fill; the one-trade-per-cycle rail holds; and
NOTHING executes under the default executor flags (enabled:false / dry_run:true)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.executor.config import ExecConfig
from src.genz import engine as eng
from src.genz.config import GenzConfig

NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
GZ = GenzConfig()


# --------------------------------------------------------------------------- #
# Fakes                                                                          #
# --------------------------------------------------------------------------- #
class _MD:
    """Read-only live-book stand-in exposing the two ask-ladder methods execute_arb + the engine use."""
    def __init__(self, ladders):
        self.ladders = ladders          # {("kalshi", ticker): ladder, ("poly", token): ladder}

    def kalshi_ask_ladder(self, ticker, side="YES"):
        return list(self.ladders.get(("kalshi", ticker), []))

    def poly_ask_ladder(self, token):
        return list(self.ladders.get(("poly", token), []))


class _NoTrade:
    """A trading client that fails loudly if anything tries to place/sell — proves dry-run places nothing."""
    def place_order(self, *a, **k):
        raise AssertionError("placed an order under dry-run!")

    def place_market_sell(self, *a, **k):
        raise AssertionError("sold under dry-run!")

    def get_positions(self):
        return []


def _node(mt, mk, side, line, kind, conf, kt, ks, pt):
    return {"market_type": mt, "market_key": mk, "side": side, "line": line, "kind": kind,
            "confidence": conf, "kalshi_ticker": kt, "kalshi_side": ks, "poly_token_id": pt,
            "poly_side": side, "outcome_label": side}


def _tree():
    """One game: a totals 2-outcome market (an arb), a team_total 2-outcome market (an arb), and a
    3-way moneyline (must be skipped)."""
    return {"games": {"G1": {"away": "Ivory Coast", "home": "Norway", "nodes": [
        _node("totals", "totals|2.5", "over", 2.5, "2way", "high", "K_OV", "YES", "P_OV"),
        _node("totals", "totals|2.5", "under", 2.5, "2way", "high", "K_UN", "NO", "P_UN"),
        _node("team_total", "team_total|norway|1.5", "over", 1.5, "2way", "high", "K_TO", "YES", "P_TO"),
        _node("team_total", "team_total|norway|1.5", "under", 1.5, "2way", "high", "K_TU", "NO", "P_TU"),
        _node("moneyline", "moneyline", "home", None, "3way", "high", "K_H", "YES", "P_H"),
        _node("moneyline", "moneyline", "draw", None, "3way", "high", "K_D", "YES", "P_D"),
        _node("moneyline", "moneyline", "away", None, "3way", "high", "K_A", "YES", "P_A"),
    ]}}}


# Deep books so walk-to-stake (stake 200) fills at the best ask. Best Over = 0.45 (kalshi),
# best Under = 0.50 (poly) -> implied 0.95 < 1 -> arb. team_total similar (0.40 + 0.55 = 0.95).
_LADDERS = {
    ("kalshi", "K_OV"): [(0.45, 100000)], ("poly", "P_OV"): [(0.48, 100000)],
    ("kalshi", "K_UN"): [(0.60, 100000)], ("poly", "P_UN"): [(0.50, 100000)],
    ("kalshi", "K_TO"): [(0.40, 100000)], ("poly", "P_TO"): [(0.44, 100000)],
    ("kalshi", "K_TU"): [(0.58, 100000)], ("poly", "P_TU"): [(0.55, 100000)],
}


# --------------------------------------------------------------------------- #
# Tests                                                                          #
# --------------------------------------------------------------------------- #
def test_collect_markets_skips_three_way():
    markets = eng.collect_markets(_tree())
    types = {m.market_type for m in markets}
    assert types == {"totals", "team_total"}                   # moneyline (3-way) excluded
    assert all(m.two_outcome for m in markets)


def test_walk_to_stake_used_for_fill_not_top_of_book():
    md = _MD({("kalshi", "THIN"): [(0.45, 10), (0.50, 100000)]})
    pv = eng._price_one(md, "kalshi", "THIN", "YES", stake=1000.0)
    assert pv.best_ask == 0.45                                  # top-of-book tick
    assert abs(pv.fill - (0.45 * 10 + 0.50 * 990) / 1000) < 1e-9   # WALK-to-stake avg (worse, real)
    assert pv.fill > pv.best_ask


def test_find_arb_best_of_both_sub_one():
    md = _MD(_LADDERS)
    markets = eng.collect_markets(_tree())
    priced = eng.price_markets(md, markets, GZ)
    arbs = eng.find_arbs(markets, priced)
    assert len(arbs) == 2 and all(a.is_arb for a in arbs)
    tot = next(a for a in arbs if a.market.market_type == "totals")
    assert tot.quote_a.venue == "kalshi" and abs(tot.quote_a.price - 0.45) < 1e-9   # best Over = kalshi
    assert tot.quote_b.venue == "polymarket" and abs(tot.quote_b.price - 0.50) < 1e-9  # best Under = poly
    assert abs(tot.implied_cost - 0.95) < 1e-9 and tot.roi_pct > 0


def test_nothing_executes_under_default_flags(tmp_path):
    """Default executor flags (enabled:false / dry_run:true) -> every arb is measured (dry-run) and
    logged, NOTHING is placed (the trading clients raise if touched)."""
    md = _MD(_LADDERS)
    exec_cfg = ExecConfig()                                     # the SAFE defaults
    assert exec_cfg.live_allowed is False
    res = eng.run_cycle(_tree(), md, GZ, exec_cfg, now=NOW, kalshi=_NoTrade(), poly=_NoTrade(),
                        arbs_path=str(tmp_path / "genz_arbs.csv"),
                        heartbeat_path=str(tmp_path / "hb.json"))
    assert res.arbs_found == 2 and res.would_trade == 2         # both survive + clear the edge floor
    assert all(r["exec_status"] == "dryrun" for r in res.rows)  # measured only
    assert (tmp_path / "genz_arbs.csv").exists() and (tmp_path / "hb.json").exists()


def test_implausible_arb_rejected_by_guard():
    """A ~900% arb (best-of-both summing to ~0.10) is a pairing bug, not a real arb. The guard must
    reject it: would_trade=False, exec_status='rejected_implausible', and it never auto-trades."""
    tree = {"games": {"G1": {"away": "Ivory Coast", "home": "Norway", "nodes": [
        _node("spread", "spread|2.5|norway", "cover", 2.5, "2way", "high", "K_C", "YES", "P_C"),
        _node("spread", "spread|2.5|norway", "plus", 2.5, "2way", "high", "K_P", "NO", "P_P"),
    ]}}}
    md = _MD({("kalshi", "K_C"): [(0.05, 100000)], ("poly", "P_C"): [(0.06, 100000)],
              ("kalshi", "K_P"): [(0.05, 100000)], ("poly", "P_P"): [(0.07, 100000)]})
    res = eng.run_cycle(tree, md, GZ, ExecConfig(), now=NOW, kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.arbs_found == 1 and res.would_trade == 0
    row = res.rows[0]
    assert row["roi_pct"] > 100 and row["implied_cost"] < 0.5      # implausible
    assert row["would_trade"] is False
    assert row["exec_status"] == "rejected_implausible"
    assert "review pairing" in row["note"]


def test_plausible_arb_not_rejected_by_guard():
    """A real low-single-digit-% arb (implied 0.95) clears the guard and is measured normally."""
    md = _MD(_LADDERS)
    res = eng.run_cycle(_tree(), md, GZ, ExecConfig(), now=NOW, kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.arbs_found == 2 and res.would_trade == 2
    assert all(r["exec_status"] == "dryrun" for r in res.rows)    # none rejected


def test_25_percent_arb_rejected_by_tightened_guard():
    """The guard default is now 8%: a 25% phantom (implied 0.80) that used to slip through is now
    rejected (would_trade=False)."""
    tree = {"games": {"G1": {"away": "A", "home": "H", "nodes": [
        _node("2h_total", "2h_total|1.5", "over", 1.5, "2way", "high", "K_O", "YES", "P_O"),
        _node("2h_total", "2h_total|1.5", "under", 1.5, "2way", "high", "K_U", "NO", "P_U"),
    ]}}}
    md = _MD({("kalshi", "K_O"): [(0.40, 100000)], ("poly", "P_O"): [(0.42, 100000)],
              ("kalshi", "K_U"): [(0.40, 100000)], ("poly", "P_U"): [(0.45, 100000)]})  # best 0.40+0.40=0.80
    res = eng.run_cycle(tree, md, GZ, ExecConfig(), now=NOW, kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.arbs_found == 1 and res.would_trade == 0
    row = res.rows[0]
    assert 20 < row["roi_pct"] < 30 and row["exec_status"] == "rejected_implausible"


def test_cycle_resilient_to_one_book_fetch_failure():
    """A book-fetch that raises for ONE node marks it unpriced and is counted — the cycle prices the
    rest and never crashes (and never feeds a half price into an arb)."""
    class _FlakyMD(_MD):
        def poly_ask_ladder(self, token):
            if token == "P_OV":
                raise TimeoutError("read timeout")
            return super().poly_ask_ladder(token)

    res = eng.run_cycle(_tree(), _FlakyMD(_LADDERS), GZ, ExecConfig(), now=NOW,
                        kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.nodes_unpriced >= 1            # P_OV failed -> unpriced
    assert res.nodes_priced >= 1             # the rest still priced; cycle did not abort
    assert res.arbs_found >= 1               # totals arb still forms from the surviving quotes


def test_market_skipped_when_one_venue_settled():
    """A node where one venue's market is settled while the other is open is desynced — skip it, never
    arb it (the half-period staleness phantom)."""
    class _SettledKalshiMD(_MD):
        def kalshi_market_open(self, ticker):
            return False                     # Kalshi settled...
        def poly_market_open(self, token):
            return True                      # ...while Poly is still open

    res = eng.run_cycle(_tree(), _SettledKalshiMD(_LADDERS), GZ, ExecConfig(), now=NOW,
                        kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.markets_skipped >= 2 and res.arbs_found == 0    # both 2-outcome markets desynced -> skipped


def test_started_game_all_nodes_skipped_before_pricing():
    """A game whose kickoff has passed has ALL its nodes skipped and counted in markets_skipped —
    BEFORE pricing (so a live game is never priced or arbed). markets_skipped > 0; zero rows."""
    started = {"games": {"CIVNOR": {"away": "Côte d'Ivoire", "home": "Norway",
                                     "kickoff_utc": "2020-01-01T00:00:00Z", "nodes": [
        _node("total_goals", "total_goals|2.5", "over", 2.5, "2way", "high", "K_OV", "YES", "P_OV"),
        _node("total_goals", "total_goals|2.5", "under", 2.5, "2way", "high", "K_UN", "NO", "P_UN"),
        _node("1h_total", "1h_total|0.5", "over", 0.5, "2way", "high", "K_HO", "YES", "P_HO"),
        _node("1h_total", "1h_total|0.5", "under", 0.5, "2way", "high", "K_HU", "NO", "P_HU"),
    ]}}}
    res = eng.run_cycle(started, _MD(_LADDERS), GZ, ExecConfig(), now=NOW,
                        kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.markets_skipped == 2 and res.arbs_found == 0   # both markets skipped
    assert res.nodes_priced == 0                             # NOT priced (skipped before pricing)
    assert res.rows == []                                    # zero CIVNOR rows


def test_correctly_priced_same_line_total_sums_to_one_no_phantom():
    """A correctly-priced same-line Over/Under (each venue internally complementary, over+under ~1)
    sums to ~1.0 best-of-both — like corners — so it is NOT a phantom arb (no row)."""
    tree = {"games": {"G1": {"away": "A", "home": "H", "nodes": [
        _node("total_goals", "total_goals|2.5", "over", 2.5, "2way", "high", "K_O", "YES", "P_O"),
        _node("total_goals", "total_goals|2.5", "under", 2.5, "2way", "high", "K_U", "NO", "P_U"),
    ]}}}
    md = _MD({("kalshi", "K_O"): [(0.55, 100000)], ("poly", "P_O"): [(0.54, 100000)],
              ("kalshi", "K_U"): [(0.47, 100000)], ("poly", "P_U"): [(0.48, 100000)]})
    markets = eng.collect_markets(tree)
    priced = eng.price_markets(md, markets, GZ)
    over = eng._best_side(markets[0].sides["over"], priced)
    under = eng._best_side(markets[0].sides["under"], priced)
    assert 0.98 < over.price + under.price < 1.05            # ~1.0, like corners (NOT 0.93)
    res = eng.run_cycle(tree, md, GZ, ExecConfig(), now=NOW, kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.arbs_found == 0                               # no phantom edge


def test_usabih_style_totals_phantom_skipped_when_game_live():
    """The USABIH 0.93 mispairing (poly over 0.63 + kalshi under 0.30) only arises on a LIVE game
    (the two venues price different states). Such a started game is skipped BEFORE pricing — the
    phantom never reaches an arb row or would_trade."""
    tree = {"games": {"USABIH": {"away": "USA", "home": "Bosnia",
                                 "kickoff_utc": "2020-01-01T00:00:00Z", "nodes": [
        _node("1h_total", "1h_total|0.5", "over", 0.5, "2way", "high", "K_O", "YES", "P_O"),
        _node("1h_total", "1h_total|0.5", "under", 0.5, "2way", "high", "K_U", "NO", "P_U"),
    ]}}}
    md = _MD({("kalshi", "K_O"): [(0.71, 100000)], ("poly", "P_O"): [(0.63, 100000)],
              ("kalshi", "K_U"): [(0.30, 100000)], ("poly", "P_U"): [(0.39, 100000)]})  # best 0.63+0.30=0.93
    res = eng.run_cycle(tree, md, GZ, ExecConfig(), now=NOW, kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.markets_skipped == 1 and res.nodes_priced == 0   # live game skipped before pricing
    assert res.arbs_found == 0 and res.rows == []              # the 7.5% phantom never becomes a row


def test_gate_fires_for_past_kickoff_with_live_markets_present():
    """PROOF (issue 1): a game whose kickoff is 1 HOUR IN THE PAST, with LIVE (priced) markets
    present, is skipped — markets_skipped > 0 and ZERO rows. Independent of any market closing."""
    now = datetime(2026, 6, 30, 20, 0, 0, tzinfo=timezone.utc)
    past = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tree = {"games": {"BELSEN": {"away": "Belgium", "home": "Senegal", "kickoff_utc": past, "nodes": [
        _node("1h_total", "1h_total|0.5", "over", 0.5, "2way", "high", "K_O", "YES", "P_O"),
        _node("1h_total", "1h_total|0.5", "under", 0.5, "2way", "high", "K_U", "NO", "P_U"),
    ]}}}
    md = _MD({("kalshi", "K_O"): [(0.71, 100000)], ("poly", "P_O"): [(0.63, 100000)],   # live books present
              ("kalshi", "K_U"): [(0.30, 100000)], ("poly", "P_U"): [(0.39, 100000)]})
    res = eng.run_cycle(tree, md, GZ, ExecConfig(), now=now, kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.markets_skipped > 0          # gate fired
    assert res.nodes_priced == 0            # skipped BEFORE pricing
    assert res.rows == []                   # zero rows for the started game


def test_debug_gate_reports_started_true_and_false():
    """The --debug-gate report shows started=True for a past kickoff and False for a future one, so
    the gate's evaluation is inspectable."""
    now = datetime(2026, 6, 30, 20, 0, 0, tzinfo=timezone.utc)
    tree = {"games": {"PAST": {"away": "A", "home": "B", "kickoff_utc": "2026-06-30T19:00:00Z", "nodes": []},
                      "FUT": {"away": "C", "home": "D", "kickoff_utc": "2026-07-01T19:00:00Z", "nodes": []}}}
    lines: list = []
    eng.debug_gate(tree, now=now, out=lines.append)
    text = "\n".join(lines)
    assert "PAST" in text and "started=True" in text
    assert "FUT" in text and "started=False" in text


def test_pregame_same_line_total_sums_to_one_no_phantom():
    """Issue 2: a correctly-priced same-line/period 1H total (each venue internally complementary)
    sums to ~1.0 best-of-both — like corners — so it is NOT a phantom (no arb)."""
    tree = {"games": {"G1": {"away": "A", "home": "H", "nodes": [
        _node("1h_total", "1h_total|0.5", "over", 0.5, "2way", "high", "K_O", "YES", "P_O"),
        _node("1h_total", "1h_total|0.5", "under", 0.5, "2way", "high", "K_U", "NO", "P_U"),
    ]}}}
    md = _MD({("kalshi", "K_O"): [(0.66, 100000)], ("poly", "P_O"): [(0.64, 100000)],
              ("kalshi", "K_U"): [(0.36, 100000)], ("poly", "P_U"): [(0.38, 100000)]})
    markets = eng.collect_markets(tree)
    priced = eng.price_markets(md, markets, GZ)
    over = eng._best_side(markets[0].sides["over"], priced)
    under = eng._best_side(markets[0].sides["under"], priced)
    assert 0.98 < over.price + under.price < 1.05           # ~1.0, like corners
    res = eng.run_cycle(tree, md, GZ, ExecConfig(), now=NOW, kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.arbs_found == 0


def test_pregame_093_totals_mispairing_rejected_not_traded():
    """Issue 2: the USABIH/FRASWE PRE-GAME 0.93 (best over 0.63 poly + best under 0.30 kalshi) is a
    period/line mispairing — a same-line Over/Under must sum ~1.0. It is rejected (would_trade=False),
    even though 7.5% slips under the 8% plausibility guard."""
    tree = {"games": {"USABIH": {"away": "USA", "home": "Bosnia", "nodes": [   # no kickoff -> pre-game
        _node("1h_total", "1h_total|0.5", "over", 0.5, "2way", "high", "K_O", "YES", "P_O"),
        _node("1h_total", "1h_total|0.5", "under", 0.5, "2way", "high", "K_U", "NO", "P_U"),
    ]}}}
    md = _MD({("kalshi", "K_O"): [(0.71, 100000)], ("poly", "P_O"): [(0.63, 100000)],
              ("kalshi", "K_U"): [(0.30, 100000)], ("poly", "P_U"): [(0.39, 100000)]})   # best 0.63+0.30=0.93
    res = eng.run_cycle(tree, md, GZ, ExecConfig(), now=NOW, kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.arbs_found == 1 and res.would_trade == 0
    row = res.rows[0]
    assert 0.92 < row["implied_cost"] < 0.94                # the 0.93 phantom
    assert row["exec_status"] == "rejected_total_mismatch" and row["would_trade"] is False


def test_loop_continues_after_a_cycle_raises(monkeypatch):
    """The --loop supervisor must survive a cycle that raises: it logs, sleeps, and continues to the
    next cycle. Only a STOP file or Ctrl-C (KeyboardInterrupt) ends it."""
    calls = []

    def _cycle(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("transient cycle error")    # first cycle blows up...
        raise KeyboardInterrupt                           # ...second ends the loop cleanly

    monkeypatch.setattr(eng, "run_cycle", _cycle)
    monkeypatch.setattr(eng.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(eng.tree_builder, "load_tree", lambda *a, **k: {"games": {}})
    monkeypatch.setattr(eng.exec_config, "stop_file_present", lambda *a, **k: False)
    try:
        eng.run_loop(GZ, ExecConfig(), interval=0, once=False, md=_MD({}))
    except KeyboardInterrupt:
        pass
    assert len(calls) == 2                                # continued past the 1st error to a 2nd cycle


def test_loop_halts_on_stop_file(monkeypatch):
    monkeypatch.setattr(eng.exec_config, "stop_file_present", lambda *a, **k: True)
    ran = []
    monkeypatch.setattr(eng, "run_cycle", lambda *a, **k: ran.append(1))
    eng.run_loop(GZ, ExecConfig(), interval=0, once=False, md=_MD({}))
    assert ran == []                                     # STOP file present -> never ran a cycle


def test_one_trade_attempt_per_cycle(monkeypatch):
    """With the flags ON (live allowed), at most ONE arb per cycle is sent live; the rest are measured
    (live=False). We record the `live` kwarg of each execute_arb call."""
    calls = []

    class _R:
        status, reason, arb_survived = "dryrun", "ok", True
        detail = {"net_edge_pct": 5.0}

    def _fake_execute(arb, *, live=False, **kw):
        calls.append(live)
        return _R()

    monkeypatch.setattr(eng.exec_engine, "execute_arb", _fake_execute)
    live_cfg = ExecConfig(enabled=True, dry_run=False, require_human_confirm=False)
    assert live_cfg.live_allowed is True
    md = _MD(_LADDERS)
    eng.run_cycle(_tree(), md, GZ, live_cfg, now=NOW, kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert len(calls) == 2                                      # two arbs evaluated
    assert sum(1 for c in calls if c) == 1                     # exactly ONE live attempt (the rail)


def test_trade_attempts_key_on_opportunity_not_csv_rows(tmp_path, monkeypatch):
    """A single arb logged on EVERY cycle (duplicate CSV rows) must NEVER count as multiple trade
    attempts: the executor rail dedups on the live OPPORTUNITY (fingerprint), not CSV rows. Four live
    cycles with a SHARED guard place the arb exactly ONCE (cooldown/fingerprint-dedupe block cycles
    2-4), even though 4 rows are appended."""
    from src.executor import config as ex_cfg
    from src.executor.guardrails import Guardrails
    from src.executor.ledger import Ledger
    from src.genz import report
    monkeypatch.setattr(ex_cfg, "stop_file_present", lambda *a, **k: False)     # no STOP in the test env

    placed = {"kalshi": [], "poly": []}

    class _KalshiExec:
        def place_order(self, ticker, side, size, limit, **kw):
            placed["kalshi"].append(ticker)
            return {"fill_count": size, "avg_price": limit}

        def get_positions(self):
            return []

    class _PolyExec:
        def place_order(self, token, limit, size, side, **kw):
            placed["poly"].append(token)
            return {"shares": size, "avg_price": limit}

    # CANMAR corners 8.5 arb (corners are NOT totals-gated): best over 0.45 + best under 0.50 = 0.95.
    tree = {"games": {"CANMAR": {"away": "Canada", "home": "Morocco", "nodes": [
        _node("corners", "corners|8.5", "over", 8.5, "2way", "high", "K_O", "YES", "P_O"),
        _node("corners", "corners|8.5", "under", 8.5, "2way", "high", "K_U", "NO", "P_U"),
    ]}}}
    md = _MD({("kalshi", "K_O"): [(0.45, 100000)], ("poly", "P_O"): [(0.48, 100000)],
              ("kalshi", "K_U"): [(0.55, 100000)], ("poly", "P_U"): [(0.50, 100000)]})
    cfg = ExecConfig(enabled=True, dry_run=False, require_human_confirm=False)
    ledger = Ledger(path=str(tmp_path / "ledger.csv"))
    guard = Guardrails(cfg, ledger=ledger, stop_path=str(tmp_path / "NO_STOP"))
    arbs_path = str(tmp_path / "genz_arbs.csv")
    for _ in range(4):
        eng.run_cycle(tree, md, GZ, cfg, now=NOW, kalshi=_KalshiExec(), poly=_PolyExec(),
                      ledger=ledger, guard=guard, write=True, arbs_path=arbs_path,
                      heartbeat_path=str(tmp_path / "hb.json"))

    import csv as _csv
    with open(arbs_path, encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    assert len(rows) == 4                                       # duplicate per-cycle rows (the readability problem)
    assert len(placed["kalshi"]) == 1 and len(placed["poly"]) == 1   # PLACED once — deduped on the opportunity
    uniq = report.aggregate(rows)
    assert len(uniq) == 1 and uniq[0]["seen_count"] == 4        # 4 rows collapse to ONE unique arb


def test_run_cycle_rotates_arbs_csv_daily(tmp_path, monkeypatch):
    """With no explicit path, a cycle appends to the DATED file genz_arbs_YYYYMMDD.csv (daily
    rotation) so a multi-day run never grows one giant file."""
    import os
    monkeypatch.setattr(eng.gz_config, "GENZ_DIR", str(tmp_path))
    tree = {"games": {"G1": {"away": "A", "home": "H", "nodes": [
        _node("corners", "corners|8.5", "over", 8.5, "2way", "high", "K_O", "YES", "P_O"),
        _node("corners", "corners|8.5", "under", 8.5, "2way", "high", "K_U", "NO", "P_U"),
    ]}}}
    md = _MD({("kalshi", "K_O"): [(0.45, 100000)], ("poly", "P_O"): [(0.48, 100000)],
              ("kalshi", "K_U"): [(0.55, 100000)], ("poly", "P_U"): [(0.50, 100000)]})
    day = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    eng.run_cycle(tree, md, GZ, ExecConfig(), now=day, kalshi=_NoTrade(), poly=_NoTrade(),
                  write=True, heartbeat_path=str(tmp_path / "hb.json"))
    assert os.path.exists(tmp_path / "genz_arbs_20260703.csv")  # dated (rotated) file
    assert not os.path.exists(tmp_path / "genz_arbs.csv")       # NOT the legacy single file


# --------------------------------------------------------------------------- #
# SETTLEMENT-PERIOD GUARD (engine backstop): a count market whose venues settle    #
# DIFFERENT periods (Kalshi full-game incl. ET vs Poly 90'+stoppage) never trades. #
# --------------------------------------------------------------------------- #
def _count_node(mt, mk, side, line, kt, ks, pt, kperiod, pperiod):
    n = _node(mt, mk, side, line, "2way", "high", kt, ks, pt)
    n["kalshi_period"], n["poly_period"] = kperiod, pperiod
    return n


_PERIOD_LADDERS = {("kalshi", "K_O"): [(0.45, 100000)], ("poly", "P_O"): [(0.48, 100000)],
                   ("kalshi", "K_U"): [(0.50, 100000)], ("poly", "P_U"): [(0.52, 100000)]}   # implied 0.95


def test_count_market_period_mismatch_rejected_by_engine_guard():
    """A corners node whose venues carry DIFFERENT settlement periods is rejected (would_trade=False,
    rejected_period_mismatch) regardless of the ~8% implied sum — both legs can lose in extra time."""
    tree = {"games": {"PORESP": {"away": "Portugal", "home": "Spain", "nodes": [
        _count_node("corners", "corners|8.5", "over", 8.5, "K_O", "YES", "P_O", "full_game", "regulation"),
        _count_node("corners", "corners|8.5", "under", 8.5, "K_U", "NO", "P_U", "full_game", "regulation"),
    ]}}}
    res = eng.run_cycle(tree, _MD(_PERIOD_LADDERS), GZ, ExecConfig(), now=NOW,
                        kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.arbs_found == 1 and res.would_trade == 0
    assert res.rows[0]["exec_status"] == "rejected_period_mismatch"
    assert res.rows[0]["would_trade"] is False


def test_count_market_same_period_is_eligible():
    """A corners node whose venues settle the SAME period passes the guard and is measured normally."""
    tree = {"games": {"CANMAR": {"away": "Canada", "home": "Morocco", "nodes": [
        _count_node("corners", "corners|8.5", "over", 8.5, "K_O", "YES", "P_O", "full_game", "full_game"),
        _count_node("corners", "corners|8.5", "under", 8.5, "K_U", "NO", "P_U", "full_game", "full_game"),
    ]}}}
    res = eng.run_cycle(tree, _MD(_PERIOD_LADDERS), GZ, ExecConfig(), now=NOW,
                        kalshi=_NoTrade(), poly=_NoTrade(), write=False)
    assert res.would_trade == 1 and res.rows[0]["exec_status"] == "dryrun"


# --------------------------------------------------------------------------- #
# FULL-MARKET SNAPSHOT: EVERY priced market each cycle (arbs AND non-arbs).       #
# --------------------------------------------------------------------------- #
def test_run_cycle_writes_full_market_snapshot(tmp_path):
    """run_cycle writes genz_snapshot.json with EVERY priced market for a game — arbs AND non-arbs
    (implied > 1.0) — plus the period fields, written ATOMICALLY (no leftover .tmp)."""
    import json
    tree = {"games": {"PORESP": {"away": "Portugal", "home": "Spain",
                                 "kickoff_utc": "2026-07-06T19:00:00Z", "nodes": [
        # an ARB (best-of-both implied 0.95)
        _node("total_goals", "total_goals|2.5", "over", 2.5, "2way", "high", "K_O", "YES", "P_O"),
        _node("total_goals", "total_goals|2.5", "under", 2.5, "2way", "high", "K_U", "NO", "P_U"),
        # a NON-ARB (best-of-both implied 1.01, ROI ~ -1%) — must still appear in the snapshot
        _node("btts", "btts", "yes", None, "2way", "high", "K_BY", "YES", "P_BY"),
        _node("btts", "btts", "no", None, "2way", "high", "K_BN", "NO", "P_BN"),
    ]}}}
    md = _MD({("kalshi", "K_O"): [(0.45, 100000)], ("poly", "P_O"): [(0.48, 100000)],
              ("kalshi", "K_U"): [(0.55, 100000)], ("poly", "P_U"): [(0.50, 100000)],
              ("kalshi", "K_BY"): [(0.55, 100000)], ("poly", "P_BY"): [(0.56, 100000)],
              ("kalshi", "K_BN"): [(0.47, 100000)], ("poly", "P_BN"): [(0.46, 100000)]})
    snap_path = tmp_path / "genz_snapshot.json"
    eng.run_cycle(tree, md, GZ, ExecConfig(), now=NOW, kalshi=_NoTrade(), poly=_NoTrade(), write=True,
                  arbs_path=str(tmp_path / "a.csv"), heartbeat_path=str(tmp_path / "hb.json"),
                  snapshot_path=str(snap_path))

    assert snap_path.exists() and not (tmp_path / "genz_snapshot.json.tmp").exists()   # atomic
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert snap["cycle_utc"] == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    g = snap["games"]["PORESP"]
    assert g["teams"] == "Portugal vs Spain" and g["started"] is False
    mkts = {m["market_type"]: m for m in g["markets"]}
    assert set(mkts) == {"total_goals", "btts"}                       # BOTH priced markets (arb + non-arb)
    assert mkts["total_goals"]["implied_cost"] < 1.0 and mkts["total_goals"]["would_trade"] is True
    assert mkts["btts"]["implied_cost"] > 1.0                         # the NON-ARB is present...
    assert mkts["btts"]["would_trade"] is False and mkts["btts"]["exec_status"] == "no_arb"
    for m in g["markets"]:                                            # required fields incl. periods + flag
        for f in ("side_a", "venue_a", "price_a", "side_b", "venue_b", "price_b", "roi_pct",
                  "kalshi_period", "poly_period", "period_mismatch"):
            assert f in m
    assert (tmp_path / "a.csv").exists()                             # genz_arbs.csv still written


# --------------------------------------------------------------------------- #
# DEPTH-AWARE STAKE SIZING: how big is the arb? (max stake before the edge dies)  #
# --------------------------------------------------------------------------- #
def test_snapshot_depth_sizing_bounded_by_thinner_leg():
    """A real arb with a known 2-level book per side: max_stake is bounded by the THINNER leg (the over
    book's cheap size runs out first), per-leg depth is the dollars fillable within the arb, and the
    profit figures are NET of the Kalshi taker fee on the over leg. A non-arb leaves the depth/stake
    fields null but STILL carries the price-only net fields."""
    from src.genz.engine import Market, PricedVenue, _kalshi_fee_rate
    na = _node("corners", "corners|8.5", "over", 8.5, "2way", "high", "K_O", "YES", "P_O")
    nb = _node("corners", "corners|8.5", "under", 8.5, "2way", "high", "K_U", "NO", "P_U")
    market = Market(game="PORESP", away="Portugal", home="Spain", market_type="corners",
                    market_key="corners|8.5", line=8.5, kind="2way", confidence="high",
                    kickoff="2026-07-06T19:00:00Z", sides={"over": na, "under": nb})
    # over (KALSHI): 100 @ 0.45 then 900 @ 0.50 ; under (POLY): 200 @ 0.50 then 800 @ 0.55
    priced = {
        ("kalshi", "K_O", "YES"): PricedVenue("kalshi", "K_O", 0.45, 0.45, 4500.0,
                                              ladder=[(0.45, 100), (0.50, 900)]),
        ("poly", "P_U", "BUY"): PricedVenue("poly", "P_U", 0.50, 0.50, 5500.0,
                                            ladder=[(0.50, 200), (0.55, 800)]),
    }
    snap = eng._market_snapshot(market, priced)
    assert snap["implied_cost"] == 0.95                                 # best-of-both top-of-book arb
    # The fee-aware marginal walk stops when the over leg steps to 0.50: 0.50 + 0.50 + fee(0.50) > 1.0
    # -> 100 equal shares: 100@0.45 (=$45, KALSHI) + 100@0.50 (=$50, POLY), fee = 100·fee(0.45).
    assert snap["max_stake_usd"] == 95.0                               # bounded by the thinner (over) leg
    assert (snap["depth_a_usd"], snap["depth_b_usd"]) == (45.0, 50.0)  # $ fillable per leg (pre-fee)
    fee_total = 100 * _kalshi_fee_rate(0.45)                           # only the over (kalshi) leg pays
    assert snap["profit_at_max_usd"] == round(100 - 95 - fee_total, 2)  # 3.27 NET (was 5.00 pre-fee)
    # $100 reference: guaranteed profit = total/implied - total - fee on those contracts (net, not gross).
    n = 100.0 / 0.95
    assert snap["profit_usd"] == round(100.0 / 0.95 - 100.0 - n * _kalshi_fee_rate(0.45), 2)  # 3.44
    assert round(snap["split_a_usd"] + snap["split_b_usd"], 2) == 100.0
    # net_roi_pct < gross roi_pct because the kalshi leg pays a fee; fee_rate_pct is that fee as % implied.
    assert snap["net_roi_pct"] < snap["roi_pct"] and snap["fee_rate_pct"] > 0.0

    # A NON-ARB (implied >= 1): depth/stake fields are null, but the price-only net fields are present.
    flat = {
        ("kalshi", "K_O", "YES"): PricedVenue("kalshi", "K_O", 0.55, 0.55, 5500.0, ladder=[(0.55, 100)]),
        ("poly", "P_U", "BUY"): PricedVenue("poly", "P_U", 0.50, 0.50, 5000.0, ladder=[(0.50, 200)]),
    }
    non_arb = eng._market_snapshot(market, flat)
    assert non_arb["implied_cost"] >= 1.0
    assert all(non_arb[k] is None for k in eng._DEPTH_FIELDS)          # depth/stake null for a non-arb...
    assert non_arb["net_roi_pct"] is not None                         # ...but net_roi_pct is still computed
    assert non_arb["fee_rate_pct"] is not None


# --------------------------------------------------------------------------- #
# FEE-HONEST SIZING: the Kalshi taker fee turns gross "arbs" into net losses.     #
# --------------------------------------------------------------------------- #
def _q(venue, price, ladder):
    from src.genz.engine import SideQuote
    return SideQuote(venue, {}, price, sum(p * s for p, s in ladder), ladder=ladder)


def test_kalshi_fee_rate_spot_values():
    """The smooth per-contract fee rate = 0.07·P·(1−P) (no ceil)."""
    assert eng._kalshi_fee_rate(0.5) == 0.0175
    assert round(eng._kalshi_fee_rate(0.96), 6) == 0.002688


def _net_roi_034():
    """net_roi_pct for the kalshi-0.34 / poly-0.6475 case (shared by two tests)."""
    return eng._sizing(_q("kalshi", 0.34, [(0.34, 10000)]),
                       _q("polymarket", 0.6475, [(0.6475, 10000)]), 0.9875)["net_roi_pct"]


def test_fee_aware_walk_stops_earlier_and_goes_net_negative():
    """kalshi 0.34 + poly 0.6475 (gross implied 0.9875 < 1 -> looks like an arb): fee-aware, the FIRST
    contract already costs > $1 net, so the walk fills NOTHING (stops earlier than the pre-fee walk),
    net_roi_pct ≈ -0.32%, and the $100 reference is a guaranteed LOSS."""
    la, lb = [(0.34, 10000)], [(0.6475, 10000)]
    shares, _, _, _ = eng._arb_max_fill(la, lb, "kalshi", "polymarket")
    pre_fee_shares, _, _, _ = eng._arb_max_fill(la, lb, "polymarket", "polymarket")  # no-fee baseline
    assert shares < pre_fee_shares and shares == 0                     # fee walk stops earlier

    sz = eng._sizing(_q("kalshi", 0.34, la), _q("polymarket", 0.6475, lb), 0.9875)
    assert -0.40 < sz["net_roi_pct"] < -0.28                          # ≈ -0.32% (net negative)
    assert sz["profit_usd"] < 0.0                                     # $100 reference is a guaranteed loss
    assert sz["profit_at_max_usd"] <= 0.0 and sz["max_stake_usd"] == 0.0  # no profitable size to deploy


def test_high_price_kalshi_leg_less_negative_than_midprice():
    """kalshi 0.96 + poly 0.039 (implied 0.999): net-negative too, but the fee at P=0.96 is tiny, so it
    is LESS negative than the 0.34 mid-price case."""
    sz = eng._sizing(_q("kalshi", 0.96, [(0.96, 10000)]), _q("polymarket", 0.039, [(0.039, 10000)]), 0.999)
    assert sz["net_roi_pct"] < 0.0                                    # still a net loss
    assert sz["net_roi_pct"] > _net_roi_034()                        # but better than the 0.34 case


def test_poly_vs_poly_net_equals_gross():
    """No Kalshi leg -> no fee -> net_roi_pct == gross roi and fee_rate_pct == 0."""
    implied = 0.95
    sz = eng._sizing(_q("polymarket", 0.45, [(0.45, 10000)]), _q("polymarket", 0.50, [(0.50, 10000)]), implied)
    gross = round((1.0 / implied - 1.0) * 100.0, 4)
    assert sz["net_roi_pct"] == gross and sz["fee_rate_pct"] == 0.0


def test_snapshot_non_arb_row_carries_net_roi():
    """Every priced snapshot row — including a NON-ARB (implied > 1) — carries net_roi_pct so the panel
    can sort all markets by net edge."""
    from src.genz.engine import Market, PricedVenue
    na = _node("btts", "btts", "yes", None, "2way", "high", "K_Y", "YES", "P_Y")
    nb = _node("btts", "btts", "no", None, "2way", "high", "K_N", "NO", "P_N")
    market = Market(game="G", away="A", home="H", market_type="btts", market_key="btts", line=None,
                    kind="2way", confidence="high", kickoff="2026-07-06T19:00:00Z",
                    sides={"yes": na, "no": nb})
    priced = {
        ("kalshi", "K_Y", "YES"): PricedVenue("kalshi", "K_Y", 0.55, 0.55, 5500.0, ladder=[(0.55, 100)]),
        ("poly", "P_N", "BUY"): PricedVenue("poly", "P_N", 0.50, 0.50, 5000.0, ladder=[(0.50, 100)]),
    }
    snap = eng._market_snapshot(market, priced)
    assert snap["implied_cost"] > 1.0                                # a non-arb
    assert snap["net_roi_pct"] is not None and snap["net_roi_pct"] < 0.0
    assert snap["max_stake_usd"] is None                             # ...yet no depth/stake sizing
