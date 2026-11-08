"""Health/trade reads must not decode unrelated bulk corporate-action evidence."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from types import SimpleNamespace

from orb_iex.dashboard.api import Journal
from orb_iex.dashboard.sources import Sources, StateJournal


def test_diagnostics_filter_applies_in_sql_before_json_decode(tmp_path, monkeypatch):
    path = tmp_path / 'state.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE diagnostics(day TEXT, kind TEXT, body TEXT)')
        db.executemany('INSERT INTO diagnostics VALUES(?,?,?)', [
            ('2026-10-28', 'divergence', '{"changed":["AAPL"]}'),
            ('2026-10-29', 'divergence', '{"changed":["MSFT"]}'),
            ('2026-10-29', 'corporate_action_snapshot', 'bulk evidence'),
        ])
    loads = json.loads

    def decode(body):
        assert body != 'bulk evidence', 'unrelated bulk payload was read'
        return loads(body)

    monkeypatch.setattr('orb_iex.dashboard.sources.json.loads', decode)
    source = StateJournal(path)
    assert len(source.diagnostics(kind='divergence')) == 2
    assert source.diagnostics('2026-10-29', kind='divergence') == [
        {'day': '2026-10-29', 'kind': 'divergence', 'body': {'changed': ['MSFT']}}]
    assert source.diagnostics(kind='absent') == []


def test_parallel_trade_reads_share_one_build_and_wal_changes_refresh(tmp_path, monkeypatch):
    state, trade = tmp_path / 'state.sqlite', tmp_path / 'trade.sqlite'
    state.touch()
    trade.touch()
    sources = SimpleNamespace(state_journal=SimpleNamespace(path=state),
                              trade_journal=SimpleNamespace(path=trade), data_dir=tmp_path)
    journal = Journal(sources, None)
    builds = []

    def build(*_):
        builds.append(object())
        sleep(0.05)
        return builds[-1]

    monkeypatch.setattr('orb_iex.dashboard.api._build_trades', build)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: journal.tradeset(), range(8)))
    assert len(builds) == 1 and all(r is results[0] for r in results)
    (tmp_path / 'health').mkdir()
    (tmp_path / 'health/heartbeat.json').write_text('{}')
    assert journal.tradeset() is results[0]  # Health does not change trade accounting.
    (tmp_path / 'state.sqlite-wal').write_bytes(b'new fill observation')
    assert journal.tradeset() is not results[0]
    assert len(builds) == 2


def test_polling_reuses_sqlite_handles_and_reads_new_commits(tmp_path, monkeypatch):
    import orb_iex.dashboard.sources as module
    sources = Sources.from_root(tmp_path)
    sources.journal_dir.mkdir(parents=True)
    for path in (sources.state_journal.path, sources.trade_journal.path):
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE observation(value TEXT)')
    opened = []
    original = module.open_ro

    def connect(path, **kwargs):
        opened.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(module, 'open_ro', connect)
    try:
        for _ in range(100):
            assert sources.state_journal.scalar('SELECT count(*) FROM observation') == 0
            assert sources.trade_journal.scalar('SELECT count(*) FROM observation') == 0
        assert len(opened) == 2
        assert sources.paper_journal is sources.paper_journal
        with sqlite3.connect(sources.state_journal.path) as writer:
            writer.execute("INSERT INTO observation VALUES('new committed data')")
        assert sources.state_journal.scalar('SELECT count(*) FROM observation') == 1
    finally:
        sources.state_journal.db().close()
        sources.trade_journal.db().close()
