/**
 * CUT コメント自動返信（Google Apps Script版）
 *
 * CUTの自動投稿アプリが作成・更新するスクリプトです。設定は Config.gs の CUT_CONFIG に
 * アプリから書き込まれます（ここを直接編集しても、次の同期で上書きされます）。
 * moment_retrieval/youtube_auto_reply.py と同じ規則で返信します:
 * 1日の上限、1回の実行で1件、返信どうしの最小間隔、定型文のランダム選択、
 * 自分のコメントと自分が返信済みのスレッドの除外、過去の未返信は古い順。
 *
 * YouTubeへは、アプリの「返信用に連携」で選んだチャンネルのログイン（Secret.gs の CUT_CREDENTIALS）で
 * アクセスします。スクリプトは管理者の個人アカウントで動くため、ブランドアカウントのチャンネルでも返信できます。
 */

var LOG_SHEET = '返信ログ';
var LOG_HEADER = ['日時', '状態', 'コメントID', '動画ID', '相手', 'コメント', '返信', 'エラー'];

/**
 * 初回の準備: 定期実行トリガーを（設定の間隔で）作り直し、ログ用シートを用意します。
 * エディタで最初に選ばれる関数なので、開いて「実行」を押すだけで準備できます（先頭に置いています）。
 */
function setup() {
  var sheet = logSheet_();
  var every = ensureTrigger_(true);
  var channel = ownChannel_();
  console.log('準備完了: ' + channel.title + ' として' + every.label + 'ごとに返信します。ログ: ' +
    sheet.getParent().getUrl());
  return {
    channelTitle: channel.title, sheetUrl: sheet.getParent().getUrl(),
    everyLabel: every.label, today: repliesToday_(sheet, new Date()),
  };
}

/** Webアプリを開いたとき: 初回の許可の後、トリガーとログを準備して状態を表示します。 */
function doGet() {
  var result = setup();
  var html = '<meta charset="utf-8"><div style="font-family:sans-serif;max-width:640px;margin:2em auto;line-height:1.7">' +
    '<h2>CUT コメント自動返信（GAS）</h2>' +
    '<p>準備ができました。このページは閉じて構いません。パソコンが起動していなくても、' +
    result.everyLabel + 'ごとに自動で確認します。</p>' +
    '<p>返信に使うチャンネル: <b>' + escapeHtml_(result.channelTitle) + '</b><br>' +
    '今日の返信: ' + result.today + '件（有効/無効や上限はアプリの設定に従います）</p>' +
    '<p><a href="' + result.sheetUrl + '" target="_blank">返信ログ（スプレッドシート）を開く</a></p></div>';
  return HtmlService.createHtmlOutput(html).setTitle('CUT コメント自動返信');
}

/** トリガーから呼ばれる本体。 */
function autoReply() {
  ensureTrigger_(false);
  var message;
  try {
    message = runOnce_(new Date());
  } catch (err) {
    message = String((err && err.message) || err).slice(0, 300);
  }
  PropertiesService.getScriptProperties().setProperty('last_message', message);
  console.log(message);
  return message;
}

function runOnce_(now) {
  var config = CUT_CONFIG;
  if (!config.enabled) return '自動返信は無効です。';
  if (!config.templates || !config.templates.length) return '定型文がありません。';
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return '別の返信処理が実行中です。';
  try {
    var sheet = logSheet_();
    var rows = logRows_(sheet);
    var doneToday = countToday_(rows, now);
    if (doneToday >= config.daily_limit) return '今日の上限（' + config.daily_limit + '件）に達しています。';
    var last = lastReplied_(rows);
    if (last && now.getTime() - last.time < config.min_gap_minutes * 60 * 1000) {
      return '前回の返信から' + config.min_gap_minutes + '分たっていないため待機します。';
    }
    var since = config.include_past ? null : new Date(config.since);
    var owner = ownChannel_();
    var handled = {};
    rows.forEach(function (row) { handled[row.commentId] = true; });
    var comment = nextCandidate_(owner.id, since, handled, config);
    if (!comment) return '返信する新しいコメントはありません。';
    var text = chooseTemplate_(config.templates, last ? last.reply : '')
      .split('{name}').join(comment.author.replace(/^@/, '')).slice(0, config.max_reply_chars);
    try {
      yt_('post', 'comments', {part: 'snippet'}, {snippet: {parentId: comment.id, textOriginal: text}});
    } catch (err) {
      if (err && err.loginExpired) throw err;  // not this comment's fault: keep it for the next run
      appendLog_(sheet, now, 'error', comment, text, String(err).slice(0, 500));
      return '返信に失敗しました（' + String(err).slice(0, 120) + '）。このコメントは今後スキップします。';
    }
    appendLog_(sheet, now, 'replied', comment, text, '');
    return '1件に返信しました（今日 ' + (doneToday + 1) + '/' + config.daily_limit + '件）。';
  } finally {
    lock.releaseLock();
  }
}

function nextCandidate_(ownerId, since, handled, config) {
  var pending = [];
  var pageToken = null;
  var pages = since ? config.max_pages_new : config.max_pages_past;
  for (var page = 0; page < pages; page++) {
    var params = {allThreadsRelatedToChannelId: ownerId, order: 'time', maxResults: 50, textFormat: 'plainText'};
    if (pageToken) params.pageToken = pageToken;
    params.part = 'snippet,replies';
    var response = yt_('get', 'commentThreads', params);
    var reachedCutoff = false;
    var items = response.items || [];
    for (var i = 0; i < items.length; i++) {
      var thread = items[i];
      var snippet = thread.snippet || {};
      var top = snippet.topLevelComment || {};
      var topSnippet = top.snippet || {};
      var published = new Date(topSnippet.publishedAt);
      if (isNaN(published.getTime())) continue;
      if (since && published < since) { reachedCutoff = true; break; }
      if (!top.id || handled[top.id]) continue;
      if (authorId_(top) === ownerId || snippet.canReply === false) continue;
      pending.push({published: published, thread: thread});
    }
    pageToken = response.nextPageToken;
    if (reachedCutoff || !pageToken) break;
  }
  pending.sort(function (a, b) { return a.published - b.published; });
  for (var j = 0; j < pending.length; j++) {
    var t = pending[j].thread;
    if (repliedByOwner_(t, ownerId)) continue;
    var s = t.snippet.topLevelComment.snippet || {};
    return {
      id: t.snippet.topLevelComment.id, videoId: t.snippet.videoId || s.videoId || '',
      author: s.authorDisplayName || '', text: s.textDisplay || s.textOriginal || '',
    };
  }
  return null;
}

function repliedByOwner_(thread, ownerId) {
  var included = (thread.replies && thread.replies.comments) || [];
  for (var i = 0; i < included.length; i++) if (authorId_(included[i]) === ownerId) return true;
  var total = Number((thread.snippet || {}).totalReplyCount || 0);
  if (total <= included.length) return false;
  var pageToken = null;
  do {
    var params = {part: 'snippet', parentId: thread.snippet.topLevelComment.id, maxResults: 100,
      textFormat: 'plainText'};
    if (pageToken) params.pageToken = pageToken;
    var response = yt_('get', 'comments', params);
    var items = response.items || [];
    for (var j = 0; j < items.length; j++) if (authorId_(items[j]) === ownerId) return true;
    pageToken = response.nextPageToken;
  } while (pageToken);
  return false;
}

function authorId_(comment) {
  var s = (comment && comment.snippet) || {};
  return (s.authorChannelId && s.authorChannelId.value) || '';
}

function chooseTemplate_(templates, last) {
  var pool = templates.filter(function (t) { return t !== last; });
  if (!pool.length) pool = templates.slice();
  return pool[Math.floor(Math.random() * pool.length)];
}

function ownChannel_() {
  var items = yt_('get', 'channels', {part: 'id,snippet', mine: true}).items || [];
  if (!items.length) throw new Error('連携したアカウントにはYouTubeチャンネルがありません。');
  return {id: items[0].id, title: (items[0].snippet || {}).title || items[0].id};
}

/** 連携したチャンネルのアクセストークン（更新トークンから取得し、約50分キャッシュ）。 */
function accessToken_() {
  var cache = CacheService.getScriptCache();
  var cached = cache.get('yt_access_token');
  if (cached) return cached;
  var creds = CUT_CREDENTIALS;
  var response = UrlFetchApp.fetch(creds.token_uri || 'https://oauth2.googleapis.com/token', {
    method: 'post', muteHttpExceptions: true,
    payload: {client_id: creds.client_id, client_secret: creds.client_secret,
      refresh_token: creds.refresh_token, grant_type: 'refresh_token'},
  });
  var data = JSON.parse(response.getContentText() || '{}');
  if (response.getResponseCode() >= 400 || !data.access_token) {
    var err = new Error('YouTubeへのログインが無効になりました（' + (data.error || response.getResponseCode()) +
      '）。パソコンのアプリで「返信用に連携」をやり直してください。');
    err.loginExpired = true;
    throw err;
  }
  cache.put('yt_access_token', data.access_token, Math.min(3000, Math.max(60, (data.expires_in || 3600) - 300)));
  return data.access_token;
}

/** YouTube Data API v3 をRESTで呼びます。 */
function yt_(method, resource, params, body) {
  var query = Object.keys(params).map(function (k) {
    return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
  }).join('&');
  var options = {method: method, muteHttpExceptions: true, headers: {Authorization: 'Bearer ' + accessToken_()}};
  if (body) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(body);
  }
  var response = UrlFetchApp.fetch('https://www.googleapis.com/youtube/v3/' + resource + '?' + query, options);
  var text = response.getContentText() || '{}';
  if (response.getResponseCode() >= 400) {
    var reason = '';
    try { reason = JSON.parse(text).error.message; } catch (e) { reason = text.slice(0, 200); }
    if (response.getResponseCode() === 401) CacheService.getScriptCache().remove('yt_access_token');
    throw new Error('YouTube API ' + response.getResponseCode() + ': ' + reason);
  }
  return JSON.parse(text);
}

/** 間隔（分）をApps Scriptのトリガーが受け付ける値に丸めます。 */
function triggerEvery_(minutes) {
  var steps = [10, 15, 30];
  for (var i = 0; i < steps.length; i++) {
    if (minutes <= steps[i]) return {minutes: steps[i], label: steps[i] + '分'};
  }
  var hours = [1, 2, 4, 6, 8, 12];
  for (var j = 0; j < hours.length; j++) {
    if (minutes <= hours[j] * 60) return {hours: hours[j], label: hours[j] + '時間'};
  }
  return {hours: 12, label: '12時間'};
}

function ensureTrigger_(force) {
  var every = triggerEvery_(CUT_CONFIG.interval_minutes);
  var props = PropertiesService.getScriptProperties();
  var key = JSON.stringify(every);
  var existing = ScriptApp.getProjectTriggers().filter(function (t) {
    return t.getHandlerFunction() === 'autoReply';
  });
  if (!force && existing.length === 1 && props.getProperty('trigger') === key) return every;
  existing.forEach(function (t) { ScriptApp.deleteTrigger(t); });
  var builder = ScriptApp.newTrigger('autoReply').timeBased();
  if (every.minutes) builder.everyMinutes(every.minutes); else builder.everyHours(every.hours);
  builder.create();
  props.setProperty('trigger', key);
  return every;
}

function logSheet_() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('log_sheet_id');
  var book = null;
  if (id) {
    try { book = SpreadsheetApp.openById(id); } catch (err) { book = null; }
  }
  if (!book) {
    book = SpreadsheetApp.create('CUT コメント自動返信ログ');
    props.setProperty('log_sheet_id', book.getId());
  }
  var sheet = book.getSheetByName(LOG_SHEET);
  if (!sheet) {
    sheet = book.getSheets()[0];
    sheet.setName(LOG_SHEET);
  }
  if (sheet.getLastRow() === 0) sheet.appendRow(LOG_HEADER);
  return sheet;
}

function logRows_(sheet) {
  var last = sheet.getLastRow();
  if (last < 2) return [];
  return sheet.getRange(2, 1, last - 1, 7).getValues().map(function (r) {
    return {time: new Date(r[0]).getTime(), status: r[1], commentId: String(r[2]), reply: String(r[6])};
  });
}

function dayKey_(date) {
  return Utilities.formatDate(date, Session.getScriptTimeZone(), 'yyyy-MM-dd');
}

function countToday_(rows, now) {
  var today = dayKey_(now);
  return rows.filter(function (r) { return r.status === 'replied' && dayKey_(new Date(r.time)) === today; }).length;
}

function repliesToday_(sheet, now) {
  return countToday_(logRows_(sheet), now);
}

function lastReplied_(rows) {
  var best = null;
  rows.forEach(function (r) { if (r.status === 'replied' && (!best || r.time > best.time)) best = r; });
  return best;
}

function appendLog_(sheet, now, status, comment, reply, error) {
  // A leading quote keeps a comment starting with "=" from being read as a formula.
  var safe = function (value) { return /^[=+\-@]/.test(value) ? "'" + value : value; };
  sheet.appendRow([now, status, safe(comment.id), safe(comment.videoId), safe(comment.author),
    safe(comment.text.slice(0, 1000)), safe(reply), error]);
}

function escapeHtml_(value) {
  return String(value).replace(/[&<>"']/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
  });
}
