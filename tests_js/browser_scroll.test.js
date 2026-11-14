// Extract and exercise the actual isolated-world page handler without Chrome.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync('chrome-extension/background.js', 'utf8');
const start = src.indexOf('function pageOperation(');
const end = src.indexOf('async function inject(', start);
assert.ok(start >= 0 && end > start);
const calls = [];
const window = {
  scrollY: 250,
  innerHeight: 800,
  scrollTo: (opts) => { calls.push(['to', opts.top]); window.scrollY = opts.top; },
  scrollBy: (opts) => { calls.push(['by', opts.top]); window.scrollY += opts.top; },
};
const sandbox = {window, document:{documentElement:{scrollHeight:5000}}};
vm.runInNewContext(src.slice(start,end), sandbox);
assert.equal(sandbox.pageOperation('browser_scroll', {direction:'top'}).verified, true);
assert.deepEqual(calls.at(-1), ['to', 0]);
assert.equal(sandbox.pageOperation('browser_scroll', {direction:'bottom'}).verified, true);
assert.deepEqual(calls.at(-1), ['to', 5000]);
assert.equal(sandbox.pageOperation('browser_scroll', {direction:'down'}).verified, true);
assert.equal(calls.at(-1)[0], 'by');
assert.throws(() => sandbox.pageOperation('browser_scroll', {direction:'sideways'}));
console.log('Chrome scroll behavior: 4 checks passed');