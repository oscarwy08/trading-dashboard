/* A closed stock market is not the red unpriced banner.
   node test/test_closed_quote.js
*/
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
function grab(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) { console.error('FAIL ' + name + ' is missing'); process.exit(1); }
  let i = src.indexOf('{', start), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) { i++; break; }
  }
  return new Function(src.slice(start, i) + '\nreturn ' + name + ';')();
}
const unpricedBanner = grab('unpricedBanner');
const positionQuoteLabel = grab('positionQuoteLabel');
const fails = [];
const check = (cond, msg) => { console.log((cond ? 'OK  ' : 'FAIL ') + msg); if (!cond) fails.push(msg); };

const closed = positionQuoteLabel({market_closed: true, unpriced: false, now: 2.87}, '£2.87');
check(closed.kind === 'closed' && closed.text === 'market closed, last close £2.87',
  'a closed market names the last close');
check(positionQuoteLabel({unpriced: true, klass: 'crypto'}, '£1').kind === 'unpriced',
  'a dead crypto quote stays unpriced');
check(positionQuoteLabel({now: 1}, '£1').kind === 'live', 'a fresh quote has no note');

const banner = unpricedBanner([
  {asset: 'ONT.L', unpriced: false, market_closed: true},
  {asset: 'FTC.L', unpriced: false, market_closed: true},
]);
check(banner === '', 'closed stocks do not raise the red banner');
const mixed = unpricedBanner([
  {asset: 'ONT.L', market_closed: true, unpriced: true},
  {asset: 'STONK', unpriced: true},
]);
check(mixed.startsWith('STONK is unpriced') && !mixed.includes('ONT.L'),
  'the banner names only a real unpriced quote');
check(src.includes('unpricedBanner(D.positions)') && src.includes('positionNow(r)'),
  'the header and the positions table use these labels');

if (fails.length) { console.error(fails.length + ' failure(s)'); process.exit(1); }
console.log('closed quote labels ok');
