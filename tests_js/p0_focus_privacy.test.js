// P0-1/P0-3: no stale Chrome page reads/edits or page-controlled error leakage.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const src = fs.readFileSync('chrome-extension/background.js', 'utf8');
const start = src.indexOf('function pageOperation(');
const end = src.indexOf('// ChatGPT handoff', start);
assert.ok(start >= 0 && end > start);

const document = {hasFocus: () => false, visibilityState:'visible'};
let currentTab = {id:7, windowId:1};
let executions = 0;
const context = {
  document,
  chrome: {
    tabs: {get: async () => ({id:7, url:'https://example.org/form', windowId:1})},
    scripting: {
      executeScript: async () => { executions++; return [{result:{ok:true}}]; },
    },
  },
  activeTab: async () => currentTab,
};
vm.runInNewContext(src.slice(start, end), context);

(async () => {
  // Run inside the actual tab's isolated world: deny background mutations,
  // snapshots and dictation before any DOM access or click.
  for (const action of ['browser_click', 'browser_fill', 'browser_scroll',
                        'dictation_insert', 'snapshot', 'read_visible', 'dictation_probe']) {
    assert.throws(() => context.pageOperation(action, {}), /前面/);
  }
  document.hasFocus = () => true;
  document.visibilityState = 'hidden';
  for (const action of ['browser_click', 'snapshot', 'read_visible'])
    assert.throws(() => context.pageOperation(action, {}), /前面/);
  document.visibilityState = 'visible';

  // A tab switch during the asynchronous bridge delivery must not dispatch
  // the previously authorized mutation into the old tab.
  currentTab = {id:8, windowId:1};
  await assert.rejects(context.inject(7,'browser_click',{id:'target'}), /切り替わった/);
  currentTab = {id:7, windowId:2};
  await assert.rejects(context.inject(7,'browser_fill',{id:'target'}), /切り替わった/);
  await assert.rejects(context.inject(7,'read_visible',{}), /切り替わった/);
  assert.equal(executions,0);
  currentTab = {id:7,windowId:1};
  assert.equal((await context.inject(7,'browser_click',{id:'target'})).ok,true);
  assert.equal(executions,1);

  // A failed snapshot must never forward the page's arbitrary exception text
  // nor private fallback tab titles/URL fragments to any model or log.
  const performStart = src.indexOf('async function perform(');
  const performEnd = src.lastIndexOf('connect();');
  assert.ok(performStart > end && performEnd > performStart);
  const privateString = 'FAKE_API_KEY_SECRET_DO_NOT_LEAK';
  const response = {
    chrome: {tabs: {query: async () => [{id:7,title:'private ' + privateString}]}},
  };
  vm.runInNewContext(`
    let snapshotTabId=null, snapshotPageKey=null, snapshotTargets=new Map();
    async function activeTab() {
      return {id:7,title:'private ${privateString}',url:'https://example.org/#${privateString}'};
    }
    async function inject() { throw Error('page error: ${privateString}'); }
    ${src.slice(performStart, performEnd)}
  `, response);
  const state = await response.perform('snapshot',{});
  assert.equal(state.error,'Chromeの画面を取得できませんでした');
  assert.equal(state.url,'');
  assert.equal(state.title,'');
  assert.equal(state.tabs.length,0);
  assert.ok(!JSON.stringify(state).includes(privateString));

  console.log('P0 Chrome focus/privacy: background access, switched tab and secret error regressions passed');
})().catch(error => { console.error(error); process.exitCode=1; });
