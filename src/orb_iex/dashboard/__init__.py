"""ORB-IEX journal dashboard: a read-only diagnostic view over the bot's own records.

Hard contract (enforced by tests, see tests/unit/test_dashboard_readonly.py):

* Nothing in this package writes to a database, a health file, a log or the broker.
  Every SQLite file is opened with the ``mode=ro`` URI flag.
* Nothing in this package imports ``broker``, ``engine``, ``execution``, ``orders`` or
  ``sim``: there is no order path and no configuration path from here into the
  trading engine. The dashboard cannot place, cancel or replace an order, and
  cannot change a strategy or risk setting.
* Missing data is reported as missing. Unknown is never rendered as zero.

Layering (see docs/PLATFORM-ARCHITECTURE.md section 3): ``sources`` -> domain modules
(``trades``, ``metrics``, ``funnel``, ``safety``, ``comparison``, ``reviews``) -> ``api``
-> ``server`` -> ``static``.
"""

from __future__ import annotations

__all__ = ["api", "comparison", "funnel", "metrics", "reviews", "safety", "server", "sources", "trades"]
