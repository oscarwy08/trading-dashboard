/* Fill markers sit on the fill price and the fill time, not the hourly bar close.
   node test/test_fill_markers.js
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
const fillMarker = grab('fillMarker');
const seriesWithFills = grab('seriesWithFills');
const fails = [];
const check = (cond, msg) => { console.log((cond ? 'OK  ' : 'FAIL ') + msg); if (!cond) fails.push(msg); };

// 12 Oct 2026 14:27 UK (BST, UTC+1) = 13:27 UTC. The old chart snapped this to the 14:00 bar.
const t = Date.parse('2026-10-12T13:27:00Z') / 1000;
const hour = t - t % 3600;
const buy = fillMarker({side: 'BUY', fill: 0.2681, t: t}, 'gbp', 0.75);
check(buy && buy.time === t && buy.time !== hour, 'the marker time is the fill time, not the hourly bar');
check(buy && buy.price === 0.2681, 'a GBP buy is drawn at the fill, so one buy sits on the avg-buy line');
check(buy && buy.position === 'atPriceBottom' && buy.shape === 'arrowUp', 'a buy arrow points up at the fill price');
const usd = fillMarker({side: 'BUY', fill: 0.2681, t: t}, 'usd', 0.75);
check(usd && Math.abs(usd.price - 0.2681 / 0.75) < 1e-12, 'a USD chart converts the GBP fill with that hour\'s rate');
const sell = fillMarker({side: 'SELL', fill: 0.30, t: t}, 'gbp', 0.75);
check(sell && sell.position === 'atPriceTop' && sell.shape === 'arrowDown' && sell.price === 0.30, 'a sell arrow points down at the fill price');
check(fillMarker({side: 'BUY', t: t}, 'gbp', 0.75) === null, 'a fill with no price is not drawn');
check(fillMarker({side: 'BUY', fill: 0.2681, t: t}, 'usd', 0) === null, 'USD with no exchange rate is not drawn');
check(!src.includes('snapT') && src.includes("position: m.position") && src.includes('price: m.price'), 'the price chart paints the fill price, not a bar snap');
const bars = [{time: hour, open: 0.255, high: 0.27, low: 0.252, close: 0.259}];
const data = seriesWithFills(bars, [{time: t}], false);
check(data.some(b => b.time === t && b.close == null) && data.some(b => b.time === hour && b.close === 0.259),
  'the fill gets its own slot and the hourly close stays 0.259');
check(seriesWithFills(bars, [{time: hour}], false).length === 1, 'a fill on a bar does not add a second point');

console.log(fails.length ? `\n${fails.length} failed` : 'fill markers ok');
process.exit(fails.length ? 1 : 0);
