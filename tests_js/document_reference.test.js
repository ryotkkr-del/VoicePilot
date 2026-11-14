// The extension exports a short-lived tab+document identity for in-memory
// matching, not the full URL or the raw page_key.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('chrome-extension/background.js','utf8');
const begin=source.indexOf('async function perform(');
const end=source.lastIndexOf('connect();');
assert.ok(begin>=0 && end>begin);
const sandbox={
  chrome:{tabs:{query:async()=>[{id:42,title:'Tab'}]}},
};
vm.runInNewContext(`
let snapshotTabId=null, snapshotPageKey=null, snapshotTargets=new Map();
async function activeTab(){return {id:42,title:'Tab',url:'https://example.org/path?secret=do-not-leak'}};
async function inject(){return {page_key:'9876',title:'Demo',url:'https://example.org/path',elements:[]}};
${source.slice(begin,end)}
`,sandbox);
(async()=>{
 const output=await sandbox.perform('snapshot',{});
 assert.equal(output.document_ref,'42:9876');
 assert.equal(output.page_key,undefined);
 assert.equal(output.url,'https://example.org/path');
 assert.equal(output.document_ref.includes('secret'),false);
 console.log('Document reference: 4 assertions passed');
})().catch(err=>{console.error(err);process.exitCode=1;});