const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');

const source = readFileSync('src/orb_iex/dashboard/static/app.js', 'utf8');
const context = vm.createContext({URLSearchParams});
vm.runInContext(source.slice(0, source.indexOf('document.addEventListener("click"')), context);
const chart = vm.runInContext('stockChart', context);

test('chart links encode only the symbol and preserve a fixed 5-minute destination', () => {
  for (const symbol of ['NASDAQ:AAPL', 'BRK.B', 'A&interval=1"/><script>alert(1)</script>']) {
    const html = chart(symbol);
    const url = new URL(html.match(/href="([^"]+)"/)[1].replaceAll('&amp;', '&'));
    assert.equal(url.origin, 'https://www.tradingview.com');
    assert.equal(url.pathname, '/chart/zOWgj5EF/');
    assert.deepEqual([...url.searchParams], [['symbol', symbol], ['interval', '5']]);
    assert.match(html, /rel="noopener noreferrer"/);
    assert.doesNotMatch(html, /<script>|<img|onclick=/);
  }
});

test('paired view keeps chart projections separate and links hypothetical and rejected signals', async () => {
  context.fixture = {
    runtime_mode: 'SHADOW', paired_day: '2026-10-28', paired: {paired_plans: {'BRK.B': {}}},
    forward: {portfolios: {
      baseline_a: {closed_trips: [{day: '2026-10-28', symbol: 'AAPL', qty: 1,
        classification: 'unknown', fills: []}], decisions: {all_signal_decisions: [
          {symbol: 'MSFT', status: 'rejected', reason: '<quote unavailable>'}]}},
      candidate_b: {closed_trips: []},
    }},
  };
  vm.runInContext('api = async () => fixture', context);
  const result = await vm.runInContext('researchPaired()', context);
  for (const symbol of ['AAPL', 'MSFT', 'BRK.B']) assert.match(result.body, new RegExp(`symbol=${symbol.replace('.', '\\.')}[^\"]*interval=5`));
  assert.match(result.body, /&lt;quote unavailable&gt;/);
  assert.match(result.body, /not imported or synchronized/);
  assert.match(result.body, /B blocks all chart entries/);
  assert.match(result.body, /No alert receiver is active/);
  assert.match(result.body, /No closed hypothetical trades yet/);
  context.fixture.forward.portfolios = {};
  context.fixture.paired = {};
  const empty = await vm.runInContext('researchPaired()', context);
  assert.match(empty.body, /No paired forward signals collected yet/);
});
