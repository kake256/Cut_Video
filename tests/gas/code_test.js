// Runs moment_retrieval/gas/Code.js in Node with fake Google services (no network).
// Invoked by tests/test_youtube_gas.py; exits non-zero on the first failed check.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const CODE = fs.readFileSync(path.join(__dirname, '..', '..', 'moment_retrieval', 'gas', 'Code.js'), 'utf8');
const OWNER = 'UC_owner';
const START = new Date('2026-10-13T03:00:00Z');

function thread(id, minutesAfter, opts = {}) {
  const replies = opts.replies || [];
  return {
    snippet: {
      videoId: 'vid1', canReply: opts.canReply !== false,
      totalReplyCount: opts.total === undefined ? replies.length : opts.total,
      topLevelComment: {id, snippet: {
        authorChannelId: {value: opts.author || 'UC_viewer'}, authorDisplayName: '@viewer',
        textDisplay: opts.text || '楽しかった',
        publishedAt: new Date(START.getTime() + minutesAfter * 60000).toISOString(),
      }},
    },
    replies: {comments: replies.map((a) => ({snippet: {authorChannelId: {value: a}}}))},
  };
}

function makeWorld(threads, config, opts = {}) {
  const rows = [];
  const props = {};
  const triggers = [];
  const inserted = [];
  const fetches = [];
  const cache = {};
  const sheet = {
    getLastRow: () => rows.length,
    appendRow: (row) => rows.push(row),
    getRange: (r, c, n, w) => ({getValues: () => rows.slice(r - 1, r - 1 + n).map((row) => row.slice(c - 1, c - 1 + w)
      .map((v) => (typeof v === 'string' && v.startsWith("'") ? v.slice(1) : v)))}),
    setName: () => {},
    getParent: () => book,
  };
  const book = {getId: () => 'sheet1', getUrl: () => 'https://sheet', getSheetByName: () => null, getSheets: () => [sheet]};
  const sandbox = {
    console: {log: () => {}},
    CUT_CONFIG: Object.assign({
      enabled: true, templates: ['ありがとう！'], daily_limit: 3, interval_minutes: 30, include_past: true,
      since: START.toISOString(), min_gap_minutes: 10, max_reply_chars: 500, max_pages_new: 2, max_pages_past: 20,
    }, config),
    CUT_CREDENTIALS: {client_id: 'cid', client_secret: 'csecret', refresh_token: 'rtoken',
      token_uri: 'https://oauth2.googleapis.com/token'},
    CacheService: {getScriptCache: () => ({
      get: (k) => (k in cache ? cache[k] : null), put: (k, v) => { cache[k] = v; }, remove: (k) => { delete cache[k]; },
    })},
    UrlFetchApp: {fetch: (url, options) => {
      fetches.push({url, options});
      const reply = (code, body) => ({getResponseCode: () => code, getContentText: () => JSON.stringify(body)});
      if (url.startsWith('https://oauth2.googleapis.com/token')) {
        assert.strictEqual(options.payload.refresh_token, 'rtoken');
        if (opts.tokenError) return reply(400, {error: 'invalid_grant'});
        return reply(200, {access_token: 'atoken', expires_in: 3599});
      }
      assert.strictEqual(options.headers.Authorization, 'Bearer atoken');
      const [resource, query] = url.replace('https://www.googleapis.com/youtube/v3/', '').split('?');
      const params = Object.fromEntries(new URLSearchParams(query));
      if (resource === 'channels') {
        assert.strictEqual(params.mine, 'true');
        return reply(200, {items: [{id: OWNER, snippet: {title: 'ゲラサブロウ'}}]});
      }
      if (resource === 'commentThreads') {
        assert.strictEqual(params.allThreadsRelatedToChannelId, OWNER);
        return reply(200, {items: threads});
      }
      if (resource === 'comments' && options.method === 'get') {
        return reply(200, {items: (opts.allReplies || {})[params.parentId] || []});
      }
      if (resource === 'comments' && options.method === 'post') {
        if (opts.insertError) return reply(403, {error: {message: opts.insertError}});
        inserted.push(JSON.parse(options.payload).snippet);
        return reply(200, {id: 'r' + inserted.length});
      }
      throw new Error('unexpected fetch ' + url);
    }},
    PropertiesService: {getScriptProperties: () => ({
      getProperty: (k) => (k in props ? props[k] : null), setProperty: (k, v) => { props[k] = v; },
    })},
    SpreadsheetApp: {create: () => book, openById: () => book},
    LockService: {getScriptLock: () => ({tryLock: () => true, releaseLock: () => {}})},
    ScriptApp: {
      getProjectTriggers: () => triggers.slice(),
      deleteTrigger: (t) => triggers.splice(triggers.indexOf(t), 1),
      newTrigger: (name) => {
        const t = {name, every: null, getHandlerFunction: () => name};
        const b = {timeBased: () => b, everyMinutes: (m) => { t.every = m + 'm'; return b; },
          everyHours: (h) => { t.every = h + 'h'; return b; }, create: () => triggers.push(t)};
        return b;
      },
    },
    Utilities: {formatDate: (d) => new Date(d.getTime() + 9 * 3600000).toISOString().slice(0, 10)},
    Session: {getScriptTimeZone: () => 'Asia/Tokyo'},
    HtmlService: {createHtmlOutput: (h) => ({html: h, setTitle() { return this; }})},
  };
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox);
  const at = (minutes) => sandbox.runOnce_(new Date(START.getTime() + minutes * 60000));
  return {sandbox, rows, inserted, triggers, fetches, at};
}

const tests = {
  oldestUnansweredFirstAndSkips() {
    const w = makeWorld([
      thread('new', 5), thread('own', -10, {author: OWNER}), thread('answered', -20, {replies: [OWNER]}),
      thread('hidden', -30, {replies: ['UC_x'], total: 9}), thread('closed', -40, {canReply: false}),
      thread('old', -2000),
    ], {}, {allReplies: {hidden: [{snippet: {authorChannelId: {value: OWNER}}}]}});
    assert.match(w.at(20), /1件/);
    assert.match(w.at(40), /1件/);
    assert.match(w.at(60), /ありません/);
    assert.deepStrictEqual(w.inserted.map((c) => c.parentId), ['old', 'new']);
  },
  newOnlyWhenPastIsOff() {
    const w = makeWorld([thread('new', 5), thread('old', -2000)], {include_past: false});
    w.at(20); w.at(40);
    assert.deepStrictEqual(w.inserted.map((c) => c.parentId), ['new']);
  },
  dailyLimitGapAndNextDay() {
    const w = makeWorld([1, 2, 3, 4].map((i) => thread('c' + i, i)), {daily_limit: 2});
    w.at(20);
    assert.match(w.at(25), /10分/);
    w.at(40);
    assert.match(w.at(80), /上限/);
    assert.strictEqual(w.inserted.length, 2);
    w.at(24 * 60 + 60);
    assert.strictEqual(w.inserted.length, 3);
  },
  disabledAndMultilineTemplate() {
    const text = '{name}さん\nまだフォローしてない人はフォローしてくれると嬉しいです';
    const off = makeWorld([thread('c1', 5)], {enabled: false, templates: [text]});
    assert.match(off.at(20), /無効/);
    const on = makeWorld([thread('c1', 5)], {templates: [text]});
    on.at(20);
    assert.strictEqual(on.inserted[0].textOriginal, 'viewerさん\nまだフォローしてない人はフォローしてくれると嬉しいです');
  },
  failureIsLoggedAndNotRetried() {
    const w = makeWorld([thread('c1', 5)], {}, {insertError: 'commentsDisabled'});
    assert.match(w.at(20), /失敗/);
    assert.strictEqual(w.rows[1][1], 'error');
    assert.match(w.at(40), /ありません/);
  },
  channelLoginIsUsedAndCached() {
    const w = makeWorld([thread('c1', 5), thread('c2', 6)]);
    w.at(20);
    w.at(40);
    const tokenCalls = w.fetches.filter((f) => f.url.startsWith('https://oauth2.googleapis.com/token'));
    assert.strictEqual(tokenCalls.length, 1);  // the access token is reused from the cache
    assert.strictEqual(w.inserted.length, 2);
  },
  expiredLoginIsReportedAndCommentIsKept() {
    const w = makeWorld([thread('c1', 5)], {}, {tokenError: true});
    const message = w.sandbox.autoReply();
    assert.match(message, /返信用に連携/);
    assert.strictEqual(w.rows.length, 1);  // header only: c1 is not marked as handled
  },
  formulaLikeTextIsKeptAsText() {
    const w = makeWorld([thread('c1', 5, {text: '=HYPERLINK("x")'})]);
    w.at(20);
    assert.strictEqual(w.rows[1][5], "'=HYPERLINK(\"x\")");
  },
  triggerIntervalsAndSetupPage() {
    const w = makeWorld([]);
    const every = (m) => JSON.stringify(w.sandbox.triggerEvery_(m));
    assert.strictEqual(every(10), JSON.stringify({minutes: 10, label: '10分'}));
    assert.strictEqual(every(31), JSON.stringify({hours: 1, label: '1時間'}));
    assert.strictEqual(every(5000), JSON.stringify({hours: 12, label: '12時間'}));
    const page = w.sandbox.doGet();
    assert.match(page.html, /ゲラサブロウ/);
    assert.strictEqual(w.triggers.length, 1);
    assert.strictEqual(w.triggers[0].every, '30m');
    w.sandbox.CUT_CONFIG.interval_minutes = 120;
    w.sandbox.autoReply();  // picks up a changed interval by itself
    assert.strictEqual(w.triggers.length, 1);
    assert.strictEqual(w.triggers[0].every, '2h');
  },
};

for (const [name, fn] of Object.entries(tests)) {
  try {
    fn();
  } catch (err) {
    console.error('FAIL ' + name + ': ' + (err && err.stack || err));
    process.exit(1);
  }
}
console.log('OK ' + Object.keys(tests).length);
