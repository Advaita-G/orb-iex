"""Fixed values. Nothing in this file is configurable at run time.

Strategy constants are copied from Main.cs, where they are `const` or
`readonly` fields. They are deliberately NOT in settings.toml, so the
ORB-IEX port cannot drift from the reference strategy by a config edit.
"""

from decimal import Decimal

VARIANT = "ORB-IEX"

# --- Alpaca endpoints ------------------------------------------------------
# The trading host is hard-coded to the PAPER host. There is no setting,
# environment variable or flag that changes it.
PAPER_TRADING_HOST = "paper-api.alpaca.markets"
PAPER_TRADING_BASE_URL = f"https://{PAPER_TRADING_HOST}"
LIVE_TRADING_HOST = "api.alpaca.markets"  # used only to recognize and refuse it
DATA_HOST = "data.alpaca.markets"
DATA_BASE_URL = f"https://{DATA_HOST}"
ALLOWED_HOSTS = frozenset({PAPER_TRADING_HOST, DATA_HOST})

MARKET_TZ = "America/New_York"

# --- Strategy constants from Main.cs ----------------------------------------
MAX_POSITIONS = 20                        # MaxPositions
LOOKBACK_DAYS = 14                        # _lookbackDays
OPENING_RANGE_MINUTES = 5                 # _openingRangeMinutes
MIN_PRICE = Decimal("5")                  # opening price > $5 (paper)
MIN_ATR = Decimal("0.50")                 # _minimumAtr (strictly greater)
MIN_AVERAGE_VOLUME = Decimal("1000000")   # _minimumAverageVolume (>=)
MIN_RELATIVE_VOLUME = Decimal("1")        # _minimumRelativeVolume (>=)
STOP_ATR_FRACTION = Decimal("0.10")       # SymbolState._stopAtrFraction
RISK_FRACTION_OF_ALLOCATION = Decimal("0.01")  # SymbolState._riskFractionOfAllocation

# --- Session schedule from Main.cs (minutes relative to the day's open/close)
OPEN_SWEEP_AFTER_OPEN_MIN = 1             # AfterMarketOpen(_spy, 1)
# Paper: positions are closed at the end of the session (4:00 pm ET). Main.cs
# used close-5 / close-2 min. ORB-IEX exits as late as Alpaca sequencing
# allows: cancel stops/entries at close-60 s, wait for confirmation (at most
# eod_cancel_timeout_seconds), market-close; final sweep at close-20 s.
# No market order is sent in the last few seconds (it could miss the close
# and be queued for the next session).
EXIT_BEFORE_CLOSE_SECONDS = 60
CLOSE_SWEEP_BEFORE_CLOSE_SECONDS = 20
NO_NEW_EXITS_BEFORE_CLOSE_SECONDS = 5
# ORB-IEX entry cutoff (DIVERGENCES AL-28). Main.cs leaves the 09:35 entry stops working all day; on 2026-09-30
# CBRS filled at 15:59:40, 20 s before the close. Unfilled entries are cancelled at `schedule.entry_cutoff_time`
# (ET) and never re-entered that day. The default 10:30 is an OPERATIONAL default so no entry order can stay live
# all day. It is NOT a claim that 10:30 is the best strategy cutoff: that is a separate research/backtest question,
# and the value stays configurable. On early-close days the cutoff is never later than this many minutes before
# the close.
ENTRY_CUTOFF_DEFAULT = "10:30"
ENTRY_CUTOFF_MIN_BEFORE_CLOSE_MIN = 30

# Alpaca Basic plan: SIP data newer than this is not available for free.
SIP_FREE_MIN_AGE_MINUTES = 15

# --- ORB-IEX runtime constants (not strategy rules) --------------------------
CLIENT_ORDER_PREFIX = "orbiex"            # client_order_id: orbiex-YYYYMMDD-SYMBOL-role
# LEAN Settings.FreePortfolioValuePercentage default, used by CalculateOrderQuantity.
LEAN_FREE_PORTFOLIO_PCT = Decimal("0.0025")
# Sessions of IEX opening bars kept for the 14-bar relative-volume average.
# Missing days are skipped (not zero-filled), so look back further than 14.
OPENING_HISTORY_SESSIONS = 30
# Sessions of regular-hours daily bars fetched for ATR(14) and ADV(14).
DAILY_HISTORY_SESSIONS = 20
# Paper universe: all stocks listed on NYSE and Nasdaq (user decision: common
# stocks and ETFs). NYSE family = NYSE, NYSE Arca (ARCA), NYSE American (AMEX).
UNIVERSE_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA", "AMEX"})
# Paper trade-update stream (same paper host; the live stream host is refused).
PAPER_STREAM_URL = f"wss://{PAPER_TRADING_HOST}/stream"
