// CUT 共有台帳の受け付け（Google Apps Script）
//
// 使い方: https://script.google.com/ で「新しいプロジェクト」を作り、このファイルの中身を
// すべて貼り付けて保存します。「デプロイ」→「新しいデプロイ」→ 種類「ウェブアプリ」で、
// 「次のユーザーとして実行: 自分」「アクセスできるユーザー: 全員」にしてデプロイし、
// 表示された「ウェブアプリのURL」（…/exec）を CUT の「分担（共有台帳）」タブに貼ります。
// 記録は自分のドライブのスプレッドシート「CUT 共有台帳」にたまります。
// URL を知っている人は誰でも書き込めるので、一緒に使う人にだけ渡してください。

var SHEET_NAME = 'ledger';
var HEADER = ['at', 'by', 'kind', 'source', 'start_sec', 'end_sec', 'title'];
var KINDS = ['claim', 'made', 'uploaded'];

function sheet_() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('SHEET_ID');
  var book = null;
  if (id) {
    try { book = SpreadsheetApp.openById(id); } catch (err) { book = null; }
  }
  if (!book) {
    book = SpreadsheetApp.create('CUT 共有台帳');
    props.setProperty('SHEET_ID', book.getId());
  }
  var sheet = book.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = book.insertSheet(SHEET_NAME);
    sheet.getRange('A:G').setNumberFormat('@');  // keep everything as plain text (no dates, no formulas)
    sheet.appendRow(HEADER);
  }
  return sheet;
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}

function text_(value, max) {
  var s = String(value === null || value === undefined ? '' : value).slice(0, max);
  return /^[=+\-@]/.test(s) ? "'" + s : s;  // never let a cell become a formula
}

function number_(value) {
  var n = Number(value);
  return value === null || value === undefined || value === '' || !isFinite(n) ? '' : String(n);
}

function doPost(e) {
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ok: false, error: 'bad request'});
  }
  var by = String(body.by || '').replace(/[\\\/:*?"<>|\s]+/g, '_').slice(0, 40);
  if (!by) return json_({ok: false, error: 'name is missing'});
  var rows = (body.entries || []).slice(0, 500).filter(function (item) {
    return item && KINDS.indexOf(item.kind) >= 0 && /^https:\/\//.test(String(item.source || ''));
  }).map(function (item) {
    return [text_(item.at, 40), text_(by, 40), item.kind, text_(item.source, 300),
            number_(item.start_sec), number_(item.end_sec), text_(item.title, 120)];
  });
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (rows.length) {
      var sheet = sheet_();
      sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, HEADER.length).setValues(rows);
    }
  } finally {
    lock.releaseLock();
  }
  return json_({ok: true, added: rows.length});
}

function doGet(e) {
  var params = (e && e.parameter) || {};
  var sheet = sheet_();
  var last = sheet.getLastRow();
  var values = last > 1 ? sheet.getRange(2, 1, last - 1, HEADER.length).getDisplayValues() : [];
  if (params.action === 'entries') {
    var source = String(params.source || '');
    var entries = values.filter(function (row) { return row[3] === source; }).map(function (row) {
      return {at: row[0], by: row[1], kind: row[2], source: row[3],
              start_sec: row[4] === '' ? null : Number(row[4]),
              end_sec: row[5] === '' ? null : Number(row[5]), title: row[6]};
    });
    return json_({ok: true, entries: entries});
  }
  var people = {};
  values.forEach(function (row) { if (row[1]) people[row[1]] = true; });
  return json_({ok: true, app: 'cut-ledger', people: Object.keys(people), count: values.length});
}
