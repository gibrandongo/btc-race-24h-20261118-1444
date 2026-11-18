"""cbBTC 1-minute trading bot for make.fun competition btc-race-24h-20261118-1444.

Protocol: agent-fun/bars-replay/v1 (newline-delimited JSON on stdin/stdout).
Reads one 1-minute cbBTC/USDC bar per batch, answers with buy/sell orders that
fill at the next bar's open. Long only, at most one open position, full size.

Strategy (two complementary legs, both genuine rules, integer math only):

  1. WASHOUT-RECLAIM (mean reversion on a sharp dip). A sharp intraday
     washout that starts to reclaim usually bounces further. Enter when the
     close is at least 100 bps below the highest high of the last 15 bars,
     has reclaimed at least 20 bps off the lowest low of the last 10 bars,
     and closed above the previous close. Exit on a +60 bps take-profit,
     a -80 bps stop, an 80 bps trailing stop, or after 90 bars.

  2. DONCHIAN BREAKOUT (trend). Enter when the close makes a new 100-bar
     high. Ride the move with an 80 bps trailing stop, a -200 bps hard stop,
     a breakeven arm (once the peak is +40 bps above entry, exit if the
     close falls back to entry), and a 300-bar maximum hold.

Risk controls: no new entries in the final 60 bars of the window (a trade
opened there would be force-flattened before it can develop); one position
at a time; full-size entries only from cash on hand; every order carries a
150 bps max-slippage guard measured from the latest close.

Determinism: pure integer arithmetic on the bars and fills the harness
sends. No network, no wall clock, no randomness, no files, no floats.
Two runs on the same data produce byte-identical replies.
"""

import json
import sys

SYMBOL = "CBBTC"

# --- units (from the protocol schema) ---
# prices: integer, scale 5. quantities: integer, scale 8.
# quote amounts: integer micro-USDC, scale 6. 1 USDC = 1,000,000 units.
MIN_NOTIONAL = 1000000  # 1 USDC, in quote units

# --- washout-reclaim leg (bps / bars) ---
WASH_LOOKBACK_HIGH = 15   # bars over which the washout drop is measured
WASH_LOOKBACK_LOW = 10    # bars over which the reclaim is measured
WASH_DROP_BPS = 100       # close at least this far below the 15-bar high
WASH_RECLAIM_BPS = 20     # close at least this far above the 10-bar low
WASH_TP_BPS = 60          # take profit above entry
WASH_STOP_BPS = 80        # hard stop below entry
WASH_TRAIL_BPS = 80       # trailing stop below the peak since entry
WASH_MAX_HOLD = 90        # bars

# --- Donchian breakout leg (bps / bars) ---
TREND_LOOKBACK = 100      # bars for the new-high breakout
TREND_TRAIL_BPS = 80      # trailing stop below the peak since entry
TREND_STOP_BPS = 200      # hard stop below entry
TREND_BE_ARM_BPS = 40     # arm the breakeven exit once the peak is this far up
TREND_MAX_HOLD = 300      # bars

# --- window / order guards ---
NO_ENTRY_AFTER_BATCH = 1380  # no new buys in the final 60 bars of a 1440-bar window
MAX_SLIPPAGE_BPS = 150       # per-order guard, measured from the latest close


def bps_below(price, bps):
    """price * (10000 - bps) / 10000, integer floor."""
    return price * (10000 - bps) // 10000


def bps_above(price, bps):
    """price * (10000 + bps) / 10000, integer floor."""
    return price * (10000 + bps) // 10000


def main():
    closes = []  # close prices, integer scale 5
    highs = []   # high prices
    lows = []    # low prices

    cash = 0     # quote units on hand (authoritative, from the account)
    qty = 0      # base units held (authoritative, from the account)

    entry_price = None  # fill price of the open position, integer scale 5
    entry_batch = None  # batch index at which the buy filled
    peak = None         # highest close since entry (entry price at fill)
    mode = None         # "W" washout leg or "T" trend leg, for the open position

    pending_buy_id = None   # client id of a buy awaiting its fill report
    pending_mode = None     # leg the pending buy belongs to
    pending_batch = None    # batch in which the pending buy was sent

    warmup_bars = 0         # bars before which the harness rejects orders

    batch_no = 0
    out = sys.stdout

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mtype = msg.get("type")

        if mtype == "hello":
            # No reply to hello; remember the warmup so no order is sent
            # before the harness starts accepting them.
            warmup_bars = int(msg.get("warmup_bars", 0) or 0)
            continue
        if mtype == "done":
            break
        if mtype != "batch":
            continue

        seq = msg.get("seq", batch_no)

        # --- fills of the previous reply's orders ---
        for fill in msg.get("fills", []):
            if fill.get("client_id") == pending_buy_id:
                if fill.get("status") == "filled":
                    entry_price = int(fill["price"])
                    entry_batch = batch_no
                    peak = entry_price
                    mode = pending_mode
                pending_buy_id = None
                pending_mode = None
                pending_batch = None
        # A fill is always reported on the batch right after the order.
        # If this batch carried no report for the pending buy, it will never
        # arrive, so drop the stale pending state instead of carrying it.
        if pending_buy_id is not None and pending_batch is not None \
                and batch_no > pending_batch:
            pending_buy_id = None
            pending_mode = None
            pending_batch = None

        # --- account (authoritative for cash and position) ---
        account = msg.get("account", {})
        cash = int(account.get("cash_quote_units", "0"))
        qty = 0
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                qty = int(pos.get("qty", "0"))
        if qty == 0:
            entry_price = None
            entry_batch = None
            peak = None
            mode = None

        # --- this batch's bar (at most one per symbol per minute) ---
        bar = None
        for cand in msg.get("bars", []):
            if cand.get("symbol") == SYMBOL:
                bar = cand
        orders = []
        if bar is not None:
            close = int(bar["c"])
            high = int(bar["h"])
            low = int(bar["l"])
            closes.append(close)
            highs.append(high)
            lows.append(low)

            if qty > 0 and entry_price is not None and peak is not None:
                if close > peak:
                    peak = close

            if qty == 0 and cash >= MIN_NOTIONAL \
                    and batch_no < NO_ENTRY_AFTER_BATCH \
                    and batch_no >= warmup_bars:
                # Washout-reclaim entry.
                if len(closes) >= WASH_LOOKBACK_HIGH + 1:
                    hi = max(highs[-(WASH_LOOKBACK_HIGH + 1):-1])
                    lo = min(lows[-(WASH_LOOKBACK_LOW + 1):-1])
                    drop_bps = (hi - close) * 10000 // hi
                    reclaim_bps = (close - lo) * 10000 // lo
                    if (drop_bps >= WASH_DROP_BPS
                            and close > closes[-2]
                            and reclaim_bps >= WASH_RECLAIM_BPS):
                        client_id = "buy-%d" % batch_no
                        orders.append({
                            "side": "buy",
                            "client_id": client_id,
                            "symbol": SYMBOL,
                            "notional_quote_units": str(cash),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        })
                        pending_buy_id = client_id
                        pending_mode = "W"
                        pending_batch = batch_no
                # Donchian breakout entry (only if no washout order sent).
                if not orders and len(closes) >= TREND_LOOKBACK + 1:
                    hi = max(highs[-(TREND_LOOKBACK + 1):-1])
                    if close > hi:
                        client_id = "buy-%d" % batch_no
                        orders.append({
                            "side": "buy",
                            "client_id": client_id,
                            "symbol": SYMBOL,
                            "notional_quote_units": str(cash),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        })
                        pending_buy_id = client_id
                        pending_mode = "T"
                        pending_batch = batch_no
            elif qty > 0 and entry_price is not None and peak is not None:
                bars_held = batch_no - entry_batch
                sell = False
                if mode == "W":
                    if bars_held >= WASH_MAX_HOLD:
                        sell = True
                    elif close >= bps_above(entry_price, WASH_TP_BPS):
                        sell = True
                    elif close < bps_below(entry_price, WASH_STOP_BPS):
                        sell = True
                    elif close < bps_below(peak, WASH_TRAIL_BPS):
                        sell = True
                else:  # trend leg
                    if bars_held >= TREND_MAX_HOLD:
                        sell = True
                    elif close < bps_below(entry_price, TREND_STOP_BPS):
                        sell = True
                    elif (peak >= bps_above(entry_price, TREND_BE_ARM_BPS)
                            and close < entry_price):
                        sell = True
                    elif close < bps_below(peak, TREND_TRAIL_BPS):
                        sell = True
                if sell:
                    orders.append({
                        "side": "sell",
                        "client_id": "sell-%d" % batch_no,
                        "symbol": SYMBOL,
                        "qty": str(qty),
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    })

        reply = {"type": "reply", "seq": seq, "orders": orders}
        out.write(json.dumps(reply, separators=(",", ":")) + "\n")
        out.flush()
        batch_no += 1


if __name__ == "__main__":
    main()
