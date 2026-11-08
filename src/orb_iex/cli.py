"""Command line.

  python -m orb_iex check-config        validate settings + key presence, no network
  python -m orb_iex check-alpaca        paper guard + read-only entitlement checks
  python -m orb_iex account-fingerprint print the account fingerprint for safety.allowed_account_fingerprints
  python -m orb_iex signal [--date D]   signals only, no orders (D = replay a past session)
  python -m orb_iex shadow [--date D]   signals + local fill simulator, no Alpaca orders
  python -m orb_iex paper               real Alpaca PAPER orders (needs enable_paper_orders)
  python -m orb_iex run                 whatever runtime.mode says
  python -m orb_iex status [--mode M]   today's journal summary
  python -m orb_iex journal-export [--date D] [--mode M]   rebuild trading-journal exports
  python -m orb_iex changelog add|list  append-only strategy change log
  python -m orb_iex health              is the bot running; Alpaca, calendar, journal, data, disk
  python -m orb_iex review [--date D]   one day's trading-journal review
  python -m orb_iex journal-serve       read-only journal dashboard on http://127.0.0.1:8787
  python -m orb_iex journal-snapshot    write the whole dashboard payload to one JSON file

Exit codes: 0 ok, 1 check failed, 2 safety stop, 3 config error (incl. .env
not 600 with --require-private-env), 4 network, 5 disk nearly full, 6 already running,
7 fatal runtime error (entries blocked, positions protected or flattened, state persisted; the supervisor
restarts the process).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from . import constants as C
from .config import ConfigError, load_settings
from .entitlements import run_entitlement_checks
from .http import ReadOnlyClient
from .marketdata import DataPolicyError
from .ops import (DiskSpaceError, InstanceLock, KeepAwake, InstanceLockError, check_disk, check_private_env,
                  health_report, review_lines)
from . import tradelog
from .tradelog import StrategyChangeLog, TradeLog, export_all, git_commit
from .logging_setup import log_event, register_secrets, setup_logging
from .runner import FatalRunnerError
from .safety import SafetyError, account_fingerprint, check_paper_account, kill_switch_active
from .secrets import CredentialsError, load_credentials, load_env_file

EXIT_OK, EXIT_FAILED, EXIT_SAFETY, EXIT_CONFIG, EXIT_NETWORK = 0, 1, 2, 3, 4
EXIT_DISK, EXIT_LOCKED, EXIT_FATAL = 5, 6, 7


def _prepare(args):
    settings = load_settings(args.config)
    if getattr(args, "require_private_env", False):
        check_private_env(args.env_file)            # unattended: .env must exist with mode 600
    logger = setup_logging(settings.data_dir, settings.log_level, settings.log_retention_days)
    for warning in load_env_file(args.env_file):
        log_event(logger, logging.WARNING, "env_file", warning)
    credentials = load_credentials()
    register_secrets(*credentials.secrets())
    log_event(logger, logging.INFO, "config", settings.config_line())
    log_event(logger, logging.INFO, "credentials", f"keys loaded {credentials!r}")
    state = "ACTIVE (new entries would be blocked)" if kill_switch_active(settings.kill_switch_file) else "off"
    log_event(logger, logging.INFO, "kill_switch", f"kill switch {state}: {settings.kill_switch_file}")
    return settings, credentials, logger


def cmd_check_config(args) -> int:
    # Configuration validation is offline and must not require account secrets.
    if getattr(args, 'require_private_env', False):
        check_private_env(args.env_file)
    print(load_settings(args.config).config_line())
    return EXIT_OK


def cmd_check_alpaca(args) -> int:
    settings, credentials, logger = _prepare(args)
    client = ReadOnlyClient(
        credentials,
        trading_per_minute=settings.trading_calls_per_minute,
        data_per_minute=settings.market_data_calls_per_minute,
        timeout_seconds=settings.request_timeout_seconds,
    )
    report = run_entitlement_checks(client, settings)
    out_dir = Path(settings.data_dir) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"entitlements-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    verdict = "PASSED" if report["passed"] else f"FAILED: {report['required_failed']}"
    log_event(logger, logging.INFO if report["passed"] else logging.ERROR, "stage1_result",
              f"Stage 1 checks {verdict}. calls={report['call_counts']} "
              f"dataRateLimit={report['data_rate_limit_header']} report={out}")
    return EXIT_OK if report["passed"] else EXIT_FAILED


def cmd_account_fingerprint(args) -> int:
    """Read-only: print the fingerprint of the account behind the keys, for safety.allowed_account_fingerprints."""
    settings, credentials, logger = _prepare(args)
    client = ReadOnlyClient(credentials, trading_per_minute=settings.trading_calls_per_minute,
                            data_per_minute=settings.market_data_calls_per_minute,
                            timeout_seconds=settings.request_timeout_seconds)
    resp = client.trading_get("/v2/account", purpose="account_fingerprint")
    if not resp.ok or not isinstance(resp.data, dict):
        raise SafetyError(f"cannot read the account (HTTP {resp.status})")
    summary = check_paper_account(resp.data, settings.expected_account_prefix)
    fp = account_fingerprint(resp.data)
    listed = fp in settings.allowed_account_fingerprints
    print(f"account ...{summary['account_number_suffix']} status {summary['status']}")
    print(f"fingerprint: {fp}  ({'on' if listed else 'NOT on'} the allowlist in {settings.source})")
    if not listed:
        print(f'add under [safety]:  allowed_account_fingerprints = ["{fp}"]')
    return EXIT_OK


def _journal_path(settings, mode: str) -> Path:
    return Path(settings.data_dir) / ("journal.sqlite" if mode == "paper" else f"{mode}.sqlite")


def cmd_paper_state(args) -> int:
    """Read-only broker truth and local state; exit 1 means the flat gate is blocked."""
    from .paper_state import inspect_paper
    settings, credentials, _ = _prepare(args)
    client = ReadOnlyClient(credentials, trading_per_minute=settings.trading_calls_per_minute,
                            data_per_minute=settings.market_data_calls_per_minute,
                            timeout_seconds=settings.request_timeout_seconds)
    report = inspect_paper(client, settings, _journal_path(settings, 'paper'))
    print(json.dumps(report, indent=2))
    return EXIT_OK if report['flat_gate'] else EXIT_FAILED


def _tradelog_paths(settings, mode: str) -> tuple[Path, Path]:
    """Trading journal file and export folder for a live mode (configurable journal.dir)."""
    return tradelog.journal_paths(settings.journal_dir, mode)


def _run_mode(args, mode: str) -> int:
    import queue

    from .broker import AlpacaPaperBroker
    from .clock import FakeClock, SystemClock
    from .journal import Journal
    from .marketdata import BarCache, MarketData, load_calendar
    from .preflight import verify_environment
    from .runner import Runner
    from .sim import SimBroker
    from .stream import TradeUpdateStream

    settings, credentials, logger = _prepare(args)
    lock = None
    if not getattr(args, "date", None):
        check_disk(settings.data_dir, settings.min_free_disk_mb)
        lock = InstanceLock(settings.data_dir).acquire()
        if lock.recovered_dead_pid:
            log_event(logger, logging.WARNING, "stale_lock",
                      f"recovered the lock left by dead pid {lock.recovered_dead_pid}; no live instance holds it")
        _graceful_sigterm()
    client = ReadOnlyClient(credentials, trading_per_minute=settings.trading_calls_per_minute,
                            data_per_minute=settings.market_data_calls_per_minute,
                            timeout_seconds=settings.request_timeout_seconds)
    summary = verify_environment(client, settings, mode)
    account_lock = InstanceLock.for_account(summary['fingerprint']).acquire() if mode == 'paper' else None
    data_dir = Path(settings.data_dir)
    data = MarketData(client, settings, BarCache(data_dir / "cache.sqlite"))
    events: queue.Queue = queue.Queue()
    equity = Decimal(str(summary.get("equity") or "0"))

    if getattr(args, "date", None):
        if mode == "paper":
            raise SafetyError("paper mode has no replay")
        day = date.fromisoformat(args.date)
        calendar = load_calendar(client, day, back_days=120, fwd_days=5)
        session = calendar.session(day)
        if session is None:
            raise ConfigError(f"{day} is not a trading session")
        if session.close > datetime.now(timezone.utc):
            raise ConfigError(f"{day} has not closed yet; replay needs a finished session")
        clock = FakeClock(session.open - timedelta(minutes=3))
        path = data_dir / "replay" / f"{day}-{mode}.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        journal = Journal(path)
        tl_path = data_dir / "replay" / f"{day}-{mode}-trade_journal.sqlite"
        for suffix in ("", "-wal", "-shm"):
            Path(str(tl_path) + suffix).unlink(missing_ok=True)
        tlog = TradeLog(tl_path, data_dir / "replay" / f"{day}-{mode}-exports")
        broker = SimBroker(events.put, equity=equity, clock=clock.now) if mode == "shadow" else None
        if broker:                                   # like Alpaca: a stop entry already crossed is a 422
            broker.quote_fn = lambda sym: data.nbbo_at(sym, clock.now())   # Alpaca's reference: consolidated NBBO
        account = (broker.get_account if broker else (lambda: summary))
        runner = Runner(settings, mode, clock, client, data, journal, broker=broker, events=events,
                        calendar=calendar, feed_prefetch=True, account_source=account, tlog=tlog)
        runner.run(until=session.close + timedelta(minutes=3))
        report = {"day": day.isoformat(), "mode": mode, "scan": journal.day(day)["scan_status"],
                  "candidates": [c["symbol"] for c in journal.candidates(day) if c["status"] not in
                                 ("filtered", "below_top20")],
                  "trades": [{"symbol": t.symbol, "dir": t.direction, "qty": str(t.planned_qty),
                              "entry": str(t.entry_price), "stop": str(t.stop_price), "state": t.state}
                             for t in journal.trades(day.isoformat()) if t.state != "foreign"],
                  "summary": journal.day(day)["summary"]}
        if broker:
            summ = report["summary"] or {}
            orders = journal.orders(day.isoformat())
            entries = [o for o in orders if o.role == "entry"]
            report["execution"] = {
                "candidates_top20": len(report["candidates"]),
                "entries_submitted": len(entries),
                "entries_crossed_skipped": summ.get("entries_crossed_skipped"),
                "entry_422_crossed_at_submit": summ.get("entry_crossed_422"),
                "entries_rejected": summ.get("entries_rejected"),
                "entries_filled_trades": sum(1 for o in entries if o.filled_qty > 0),
                "duplicate_stops": sum(1 for n in broker.max_live_stops.values() if n > 1),
                "stop_replace_failures": summ.get("stop_replace_failed"),
                "cannot_replace_errors": broker.replace_rejected_state,
                "sim_alpaca_crossed_422": broker.crossed_rejections,      # market-caused: price already through the trigger
                "spread_too_wide_skipped": summ.get("spread_too_wide_skipped"),
                "protective_stop_crossed_flattened": summ.get("protective_stop_crossed"),
                "position_guard_actions": summ.get("position_guard_actions"),
                "postclose_exits": summ.get("postclose_exits"),
                "stop_replaces_sent": sum(1 for w in broker.write_log if w[0] == "replace"),
                "final_positions": {k: str(v) for k, v in broker.list_positions().items()}}
        if broker:
            fills = journal.fills(day.isoformat())
            commission = sum((Decimal(f["commission"]) for f in fills), Decimal(0))
            slippage = sum((Decimal(f["slippage"]) for f in fills), Decimal(0))
            gross = broker.mark_to_market()
            report["gross_pnl"] = str(gross.quantize(Decimal("0.01")))
            report["net_pnl_paper_costs"] = str((gross - commission).quantize(Decimal("0.01")))
            report["net_pnl_realistic"] = str((gross - commission - slippage).quantize(Decimal("0.01")))
            report["modeled_commission"] = str(commission)
            report["modeled_slippage"] = str(slippage)
            report["open_positions_after_close"] = {k: str(v) for k, v in broker.list_positions().items()}
        out = data_dir / "reports" / f"replay-{day}-{mode}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str))
        log_event(logger, logging.INFO, "replay_result", f"replay {day} {mode}: {json.dumps(report, default=str)[:1500]} "
                  f"report={out}")
        return EXIT_OK if report["scan"] == "ok" else EXIT_FAILED

    clock = SystemClock()
    journal = Journal(_journal_path(settings, mode))
    stream = None
    if mode == "paper":
        if settings.research_profile == "candidate_b":
            raise SafetyError("candidate_b is unqualified; paper arming refused")
        broker = AlpacaPaperBroker(client, settings.kill_switch_file)
        broker.arm(mode=mode, enable_paper_orders=settings.enable_paper_orders, account_verified=True)
        stream = TradeUpdateStream(credentials, events)
    elif mode == "shadow":
        broker = SimBroker(events.put, equity=equity, clock=clock.now, auto_fill_market=True)
    else:
        broker = None
    tl_path, tl_exports = _tradelog_paths(settings, mode)
    tlog = TradeLog(tl_path, tl_exports)
    if getattr(args, 'paper_auto', False):
        from .paper_auto import AutoPaperRunner
        runner = AutoPaperRunner(settings, mode, clock, client, data, journal, broker=broker,
                                 events=events, stream=stream, tlog=tlog, verified_account=summary)
    else:
        runner = Runner(settings, mode, clock, client, data, journal, broker=broker, events=events, stream=stream,
                        tlog=tlog)
    keep_awake = KeepAwake()
    awake = keep_awake.start() if mode != "signal" else "not_needed"
    if awake == "active":
        log_event(logger, logging.INFO, "keep_awake", "sleep prevention on (caffeinate -i -s); closing the lid on battery still sleeps the Mac")
    elif awake == "unavailable":
        log_event(logger, logging.ERROR, "keep_awake", "caffeinate is unavailable: the Mac may sleep through the scan window")
    log_event(logger, logging.WARNING if mode == "paper" else logging.INFO, "start",
              f"ORB-IEX {mode} mode running (Ctrl+C to stop). State: {journal.path}; trading journal: {tl_path}")
    try:
        runner.run()
    except KeyboardInterrupt:
        log_event(logger, logging.WARNING, "stop",
                  "stopped (Ctrl+C or service stop): unfilled ORB-IEX entries were cancelled and confirmed (see "
                  "health/shutdown.json); open positions keep their protective stops; state is saved, restart resumes")
    except FatalRunnerError as exc:
        log_event(logger, logging.CRITICAL, "exit_fatal",
                  f"exiting non-zero after a fatal error ({exc}); the supervisor restarts the process"
                  if not exc.safety else f"safety stop after a fatal error ({exc}); no automatic restart")
        return EXIT_SAFETY if exc.safety else EXIT_FATAL
    finally:
        keep_awake.stop()
        if account_lock:
            account_lock.release()
        if lock:
            lock.release()
    return EXIT_OK


def _graceful_sigterm() -> None:
    """systemd stop sends SIGTERM: turn it into the same clean stop as Ctrl+C."""
    import signal

    def handler(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:
        pass                                          # not in the main thread (tests)


def cmd_health(args) -> int:
    settings = load_settings(args.config)
    client = None
    try:
        load_env_file(args.env_file)
        credentials = load_credentials()
        register_secrets(*credentials.secrets())
        client = ReadOnlyClient(credentials, trading_per_minute=settings.trading_calls_per_minute,
                                data_per_minute=settings.market_data_calls_per_minute,
                                timeout_seconds=settings.request_timeout_seconds, max_attempts=2)
    except (CredentialsError, SafetyError) as exc:
        print(f"FAIL  alpaca keys: {exc}")
    rows = health_report(settings, client)
    for name, ok, detail in rows:
        print(f"{'OK  ' if ok else 'FAIL'}  {name}: {detail}")
    return EXIT_OK if all(ok for _, ok, _ in rows) and client is not None else EXIT_FAILED


def cmd_review(args) -> int:
    from .tradelog import summarize
    settings = load_settings(args.config)
    path, exports = _tradelog_paths(settings, args.mode)
    if not path.exists():
        print(f"no trading journal at {path}")
        return EXIT_OK
    tlog = TradeLog(path, exports)
    day = args.date or (tlog.db.execute("SELECT max(day) FROM (SELECT day FROM candidates UNION "
                                        "SELECT day FROM outcomes)").fetchone()[0])
    if not day:
        print("the trading journal has no days yet")
        return EXIT_OK
    outcomes = tlog.rows("outcomes", "WHERE day=?", (day,))
    cands = tlog.rows("candidates", "WHERE day=?", (day,))
    for line in review_lines(summarize(outcomes, cands, day), day):
        print(line)
    return EXIT_OK


def cmd_signal(args) -> int:
    return _run_mode(args, "signal")


def cmd_shadow(args) -> int:
    return _run_mode(args, "shadow")


def cmd_paper(args) -> int:
    return _run_mode(args, "paper")


def cmd_paper_auto(args) -> int:
    """One regular PAPER session, after verified startup recovery; never removes KILL."""
    args.paper_auto = True
    args.require_private_env = True
    try:
        return _run_mode(args, 'paper')
    except Exception as exc:
        # Startup failures precede construction of the runtime report writer.
        import re
        from .paper_auto import write_report
        settings = load_settings(args.config)
        path = Path(settings.data_dir) / 'health/paper-auto-startup-failure.json'
        write_report(path, {'command':'paper-auto', 'status':'FAILED CLOSED',
            'startup_at':datetime.now(timezone.utc).isoformat(), 'entry_gate_open':False,
            'reason': re.sub(r'\b[0-9a-f]{16}\b', '[fingerprint]', str(exc)),
            'error_type':type(exc).__name__})
        raise


def cmd_paper_supervise(args) -> int:
    from . import supervisor
    return supervisor.run(args.config, args.env_file, load_settings(args.config).data_dir)


def cmd_run(args) -> int:
    settings = load_settings(args.config)
    if settings.mode == "readonly":
        return cmd_check_alpaca(args)
    args.date = None
    return _run_mode(args, settings.mode)


def cmd_journal_export(args) -> int:
    settings = load_settings(args.config)
    path, exports = _tradelog_paths(settings, args.mode)
    if not path.exists():
        print(f"no trading journal at {path}")
        return EXIT_OK
    tlog = TradeLog(path, exports)
    days = [date.fromisoformat(args.date)] if args.date else sorted(
        {date.fromisoformat(r[0]) for r in tlog.db.execute("SELECT day FROM candidates UNION SELECT day FROM outcomes")})
    for d in days:
        paths = export_all(tlog, d)
        print(f"{d}: {paths['summary_md']}")
    return EXIT_OK


def cmd_changelog(args) -> int:
    settings = load_settings(args.config)
    log_ = StrategyChangeLog(Path(settings.journal_dir) / "strategy_changes.sqlite")
    if args.action == "add":
        missing = [n for n in ("rule", "old", "new", "reason", "evidence") if not getattr(args, n)]
        if missing:
            print(f"CONFIG ERROR: changelog add needs --{' --'.join(missing)}", file=sys.stderr)
            return EXIT_CONFIG
        seq = log_.add(change_date=args.change_date or date.today().isoformat(),
                       git_commit=args.commit or git_commit(), rule_changed=args.rule, old_value=args.old,
                       new_value=args.new, reason=args.reason, test_evidence=args.evidence)
        print(f"strategy change #{seq} recorded")
    else:
        for e in log_.entries():
            print(f"#{e['seq']} {e['change_date']} {e['git_commit']} {e['rule_changed']}: {e['old_value']} -> "
                  f"{e['new_value']} | {e['reason']} | evidence: {e['test_evidence']}")
    return EXIT_OK


def _journal_dashboard(args):
    """(Journal, settings, sources). Read-only: the dashboard opens every database with mode=ro.

    ``--mode`` selects which trading journal is read. It is NOT taken from settings.runtime.mode:
    that key defaults to "readonly" (what `run` does), which is not a journal the bot writes,
    so the paper journal would be looked for in the wrong file and silently read as missing.
    """
    from .dashboard.api import Journal
    from .dashboard.sources import Sources
    settings = load_settings(args.config)
    mode = getattr(args, "mode", None) or "paper"
    sources = Sources.from_settings(settings, root=Path.cwd(), mode=mode)
    return Journal(sources, settings), settings, sources


def cmd_journal_serve(args) -> int:
    from .dashboard.server import serve
    journal, _, sources = _journal_dashboard(args)
    print(f"ORB-IEX journal {journal.settings.variant} mode={sources.mode}")
    print(f"  state journal: {sources.state_journal.path}")
    print(f"  trade journal: {sources.trade_journal.path}")
    serve(journal, args.host, args.port, open_browser=args.open)
    return EXIT_OK


def cmd_daily_gate(args) -> int:
    """Print the sessions still owed a daily review, oldest first (read-only, offline)."""
    from datetime import datetime, timezone
    from .daily_gate import pending_days
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for p in pending_days(now, Path(args.reports_dir)):
        print(p.day)
    return EXIT_OK


def cmd_research_ledger(args) -> int:
    """Validate the experiment ledger and print the ranked backlog (read-only)."""
    from .research_ledger import ResearchLedger
    book = ResearchLedger(args.path).read()
    for e in book["errors"]:
        print(f"line {e['line']}: {e['error']}", file=sys.stderr)
    s = book["summary"]
    print(f"ledger {book['state']}: {s['hypotheses']} hypotheses ({s['open']} open, {s['rejected']} rejected, "
          f"{s['promotion_candidates']} promotion candidates), {s['experiments']} experiments")
    for h in book["backlog"]:
        print(f"  #{h['priority']} {h['id']} [{h['status']}] {h['title']}")
    return EXIT_CONFIG if book["errors"] else EXIT_OK


def cmd_journal_snapshot(args) -> int:
    """Every endpoint in one file, for a durable record or an external consumer."""
    journal, settings, sources = _journal_dashboard(args)
    payload = journal.snapshot()
    out = Path(args.out) if args.out else Path(settings.data_dir) / "reports" / "journal-snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"journal snapshot written to {out}")
    count = payload["trades"]["total"]
    print(f"  trades={count} funnel_day={payload['opportunities']['day']} "
          f"queue={len(payload['overview']['investigation'])}")
    return EXIT_OK


def cmd_status(args) -> int:
    from .journal import Journal
    settings = load_settings(args.config)
    path = _journal_path(settings, args.mode)
    if not path.exists():
        print(f"no journal at {path}")
        return EXIT_OK
    j = Journal(path)
    days = [r[0] for r in j.db.execute("SELECT day FROM days ORDER BY day DESC LIMIT 1")]
    if not days:
        print("journal is empty")
        return EXIT_OK
    day = date.fromisoformat(days[0])
    st = j.day(day)
    print('Local journal snapshot; current broker positions/orders require reconciliation.')
    print(f'  persistent_breakers={j.breakers()} unresolved_intents={len(j.intents(True))}')
    print(f"{C.VARIANT} {args.mode} {day}: scan={st['scan_status']} eod={st['eod_status']} done={sorted(st['done'])}")
    for t in j.trades(day.isoformat()):
        print(f"  {t.symbol:<6} dir={t.direction:+d} qty={t.planned_qty} entry={t.entry_price} stop={t.stop_price} "
              f"state={t.state} closed={t.closed}")
    if st["summary"]:
        print(f"  summary: {st['summary']}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orb_iex", description=f"{C.VARIANT} Alpaca paper-only bot")
    parser.add_argument("--config", default="config/settings.toml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--require-private-env", action="store_true",
                        help="unattended mode: refuse to start unless the .env file exists with mode 600")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config").set_defaults(func=cmd_check_config)
    sub.add_parser("check-alpaca").set_defaults(func=cmd_check_alpaca)
    sub.add_parser("paper-state", help="read-only PAPER broker/journal acceptance snapshot") \
        .set_defaults(func=cmd_paper_state)
    sub.add_parser("account-fingerprint", help="print this account's fingerprint for the safety allowlist") \
        .set_defaults(func=cmd_account_fingerprint)
    for name, func, replay in (("signal", cmd_signal, True), ("shadow", cmd_shadow, True),
                               ("paper", cmd_paper, False)):
        p = sub.add_parser(name)
        if replay:
            p.add_argument("--date", help="replay a finished session (YYYY-MM-DD) instead of running live")
        p.set_defaults(func=func)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("paper-auto", help="one PAPER session after gated recovery; external KILL is never removed") \
        .set_defaults(func=cmd_paper_auto)
    sub.add_parser("paper-supervise", help="relaunch paper-auto each session with backoff; keeps the Mac awake") \
        .set_defaults(func=cmd_paper_supervise)
    je = sub.add_parser("journal-export", help="rebuild CSV/Markdown exports from the trading journal")
    je.add_argument("--mode", default="paper", choices=["signal", "shadow", "paper"])
    je.add_argument("--date", help="YYYY-MM-DD (default: every day in the journal)")
    je.set_defaults(func=cmd_journal_export)
    cl = sub.add_parser("changelog", help="append-only strategy change log (written by a person)")
    cl.add_argument("action", choices=["add", "list"])
    for opt in ("rule", "old", "new", "reason", "evidence", "commit"):
        cl.add_argument(f"--{opt}")
    cl.add_argument("--date", dest="change_date")
    cl.set_defaults(func=cmd_changelog)
    sub.add_parser("health", help="bot running, Alpaca reachable, journal writable, data freshness, disk") \
        .set_defaults(func=cmd_health)
    rv = sub.add_parser("review", help="print a day's trading-journal review")
    rv.add_argument("--mode", default="paper", choices=["signal", "shadow", "paper"])
    rv.add_argument("--date", help="YYYY-MM-DD (default: latest day in the journal)")
    rv.set_defaults(func=cmd_review)
    st = sub.add_parser("status")
    st.add_argument("--mode", default="paper", choices=["signal", "shadow", "paper"])
    st.set_defaults(func=cmd_status)
    js = sub.add_parser("journal-serve", help="read-only journal dashboard (localhost, no writes)")
    js.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    js.add_argument("--port", type=int, default=8787)
    js.add_argument("--mode", default="paper", choices=["signal", "shadow", "paper"])
    js.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    js.set_defaults(func=cmd_journal_serve)
    jn = sub.add_parser("journal-snapshot", help="write the whole dashboard payload to one JSON file")
    jn.add_argument("--out", help="output path (default <data_dir>/reports/journal-snapshot.json)")
    jn.add_argument("--mode", default="paper", choices=["signal", "shadow", "paper"])
    jn.set_defaults(func=cmd_journal_snapshot)
    dg = sub.add_parser("daily-gate", help="sessions still owed a daily review (offline, read-only)")
    dg.add_argument("--reports-dir", default="runtime/reports")
    dg.add_argument("--now", help="ISO time override for tests")
    dg.set_defaults(func=cmd_daily_gate)
    rl = sub.add_parser("research-ledger", help="validate docs/experiment-ledger.jsonl and print the backlog")
    rl.add_argument("--path", default="docs/experiment-ledger.jsonl")
    rl.set_defaults(func=cmd_research_ledger)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SafetyError as exc:
        print(f"SAFETY STOP: {exc}", file=sys.stderr)
        return EXIT_SAFETY
    except FatalRunnerError as exc:                      # replay runs (no supervisor): same mapping as live
        print(f"FATAL: {exc}", file=sys.stderr)
        return EXIT_SAFETY if exc.safety else EXIT_FATAL
    except (ConfigError, CredentialsError, PermissionError) as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except DiskSpaceError as exc:
        print(f"DISK STOP: {exc}", file=sys.stderr)
        return EXIT_DISK
    except InstanceLockError as exc:
        print(f"ALREADY RUNNING: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except DataPolicyError as exc:
        print(f"DATA POLICY STOP: {exc}", file=sys.stderr)
        return EXIT_SAFETY
    except ConnectionError as exc:
        print(f"NETWORK ERROR: {exc}. Check the internet connection and retry.", file=sys.stderr)
        return EXIT_NETWORK
