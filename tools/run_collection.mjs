// Executes this collection's requests and actual scripts; not a general Postman/Newman implementation.
// Node 20+; no npm dependencies. Example: API_KEY=... node tools/run_collection.mjs http://localhost:8000
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import http from 'node:http';

const collection = JSON.parse(fs.readFileSync(new URL('../postman/Setu-Reconciliation.postman_collection.json', import.meta.url), 'utf8'));
const variables = new Map(collection.variable.map(v => [v.key, v.value]));
variables.set('baseUrl', (process.argv[2] || process.env.BASE_URL || 'http://localhost:8000').replace(/\/$/, ''));
variables.set('apiKey', process.env.API_KEY || '');
let assertions = 0;
function expand(text) {
  return text.replace(/\{\{([^}]+)\}\}/g, (_, key) => {
    if (key === '$guid') return randomUUID();
    if (!variables.has(key)) throw new Error(`Missing variable ${key}; run the entire collection`);
    return variables.get(key);
  });
}
const pm = {
  collectionVariables: { get: k => variables.get(k), set: (k,v) => variables.set(k,v) },
  variables: { replaceIn: expand },
  test: (name, fn) => { fn(); assertions++; },
  expect: actual => ({to: {eql: expected => assert.deepStrictEqual(actual, expected), include: expected => assert.ok(actual.includes(expected))}}),
};
function scripts(item, phase) {
  for (const hook of item.event || []) {
    if (hook.listen === phase) vm.runInNewContext(hook.script.exec.join('\n'), {pm, console}, {timeout: 5000});
  }
}
async function send(url, options) {
  if (!process.env.SOCKET_PATH) return fetch(url, options);
  // Useful for local Gunicorn/proxy diagnostics without a TCP listener.
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const req = http.request({socketPath:process.env.SOCKET_PATH, path:parsed.pathname+parsed.search,
      method:options.method, headers:options.headers, timeout:15000}, response => {
      let text='';
      response.setEncoding('utf8');
      response.on('data', chunk => { text+=chunk; });
      response.on('error', reject);
      response.on('end', () => resolve({status:response.statusCode, json:async()=>JSON.parse(text)}));
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('Request timeout')));
    req.end(options.body);
  });
}
const results=[];
for (const item of collection.item) {
  scripts(item, 'prerequest');
  const req=item.request;
  const headers=Object.fromEntries((req.header || []).map(h => [h.key, expand(h.value)]));
  if (req.auth?.type !== 'noauth') headers['X-API-Key']=variables.get('apiKey');
  const response=await send(expand(req.url), {method:req.method, headers,
    body:req.body ? expand(req.body.raw) : undefined, signal:AbortSignal.timeout(15000)});
  const body=await response.json();
  pm.response={code:response.status, json:()=>body};
  try { scripts(item, 'test'); }
  catch (error) { console.error(item.name, response.status, JSON.stringify(body)); throw error; }
  results.push({name:item.name,status:response.status});
}
console.log(JSON.stringify({requests:results.length, passed_tests:assertions, results}, null, 2));
