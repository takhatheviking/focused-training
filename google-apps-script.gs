/**
 * ============================================================
 *  Focused · Moliyaviy Savodxonlik — приёмник заявок
 *  Google Apps Script Web App
 * ============================================================
 *
 *  ИНСТРУКЦИЯ:
 *  1. Открой https://sheets.google.com — создай новую таблицу
 *     (например, "Focused — Moliyaviy Savodxonlik Zayavki")
 *  2. Расширения → Apps Script
 *  3. Удали стартовый код, вставь этот файл целиком
 *  4. Сохрани (Ctrl+S). Имя проекта: "Focused Apply Receiver"
 *  5. Запусти ОДИН РАЗ функцию setupSheet (выпадашка сверху →
 *     setupSheet → Run). Разреши доступ.
 *     → Создаст лист "Zayavkalar" с заголовками и форматированием.
 *  6. Deploy → New deployment:
 *       - Type: Web app
 *       - Description: Focused Apply v1
 *       - Execute as: Me
 *       - Who has access: Anyone   ← ВАЖНО
 *  7. Скопируй Web app URL → вставь в config.js фронтенда
 *
 *  Тест: запусти testSubmission — появится тестовая строка.
 * ============================================================
 */

const SHEET_NAME = 'Zayavkalar';
const TIMEZONE = 'Asia/Tashkent';

const HEADERS = [
  'Sana',                 // Дата подачи
  'Til',                  // Язык (UZ/RU)
  'Tadbir',               // Событие
  'FIO',                  // Имя
  'Telefon',              // Телефон
  'Aloqa kanali',         // Канал связи
  'Manba',                // Откуда узнал (source)
  'Moliyaviy muammo',     // Главная финансовая проблема (pain)
  'UTM source',
  'UTM medium',
  'UTM campaign',
  'Referrer',
  'User agent',
  'Status'                // Статус лида
];

function payloadToRow(p) {
  const date = p.submittedAt
    ? Utilities.formatDate(new Date(p.submittedAt), TIMEZONE, 'yyyy-MM-dd HH:mm')
    : Utilities.formatDate(new Date(), TIMEZONE, 'yyyy-MM-dd HH:mm');

  return [
    date,
    p.lang || '',
    p.event || '',
    p.name || '',
    p.phone || '',
    p.channel || '',
    p.source || '',
    p.pain || '',
    p.utm_source || '',
    p.utm_medium || '',
    p.utm_campaign || '',
    p.referrer || '',
    p.userAgent || '',
    'Yangi' // "Новая"
  ];
}

// ============================================================
// SETUP — запусти один раз вручную
// ============================================================
function setupSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
  }

  if (sheet.getRange(1, 1).getValue() === '') {
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
  }

  formatHeader_(sheet);
  setColumnWidths_(sheet);
  sheet.setFrozenRows(1);

  const maxCols = sheet.getMaxColumns();
  if (maxCols > HEADERS.length) {
    sheet.deleteColumns(HEADERS.length + 1, maxCols - HEADERS.length);
  }

  setupStatusFormatting_(sheet);

  Logger.log('✓ Лист "' + SHEET_NAME + '" готов. Проверь: Deploy → Web app → Anyone.');
}

function formatHeader_(sheet) {
  const headerRange = sheet.getRange(1, 1, 1, HEADERS.length);
  headerRange
    .setBackground('#000000')      // Focused brand black
    .setFontColor('#32a064')       // Focused brand green
    .setFontWeight('bold')
    .setFontSize(11)
    .setFontFamily('Manrope')
    .setHorizontalAlignment('left')
    .setVerticalAlignment('middle')
    .setWrap(true);
  sheet.setRowHeight(1, 44);
}

function setColumnWidths_(sheet) {
  const widths = {
    1: 130,  // Sana
    2: 60,   // Til
    3: 220,  // Tadbir
    4: 180,  // FIO
    5: 150,  // Telefon
    6: 120,  // Aloqa kanali
    7: 140,  // Manba
    8: 320,  // Moliyaviy muammo (текст)
    9: 120,  // UTM source
    10: 100, // UTM medium
    11: 140, // UTM campaign
    12: 180, // Referrer
    13: 200, // User agent
    14: 110  // Status
  };
  Object.keys(widths).forEach(col => {
    sheet.setColumnWidth(parseInt(col, 10), widths[col]);
  });
}

function setupStatusFormatting_(sheet) {
  const statusCol = HEADERS.length;
  const range = sheet.getRange(2, statusCol, sheet.getMaxRows() - 1, 1);

  // Убираем старые правила статуса
  const allRules = sheet.getConditionalFormatRules().filter(r => {
    const rs = r.getRanges();
    return !rs.some(x => x.getColumn() === statusCol);
  });

  const rules = [
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('Yangi')
      .setBackground('#fff4cc').setFontColor('#7a5b00')
      .setRanges([range]).build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('Bogʻlanildi')
      .setBackground('#cfe2ff').setFontColor('#0a3d82')
      .setRanges([range]).build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('Toʻlangan')
      .setBackground('#32a064').setFontColor('#ffffff')
      .setRanges([range]).build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('Rad etildi')
      .setBackground('#ffd6d6').setFontColor('#8b0000')
      .setRanges([range]).build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('No-show')
      .setBackground('#e8e8e8').setFontColor('#555555')
      .setRanges([range]).build()
  ];

  sheet.setConditionalFormatRules(allRules.concat(rules));

  const validation = SpreadsheetApp.newDataValidation()
    .requireValueInList(['Yangi', 'Bogʻlanildi', 'Toʻlangan', 'Rad etildi', 'No-show'], true)
    .setAllowInvalid(false)
    .build();
  range.setDataValidation(validation);
}

// ============================================================
// HTTP handlers
// ============================================================
function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    appendApplication_(payload);
    return jsonResponse_({ status: 'ok' });
  } catch (err) {
    Logger.log('doPost error: ' + err);
    return jsonResponse_({ status: 'error', message: String(err) });
  }
}

function doGet() {
  return jsonResponse_({
    status: 'ok',
    message: 'Focused Apply Receiver is alive',
    expecting: 'POST with JSON body'
  });
}

function appendApplication_(payload) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) throw new Error('Sheet "' + SHEET_NAME + '" not found. Run setupSheet() first.');

  const row = payloadToRow(payload);
  sheet.appendRow(row);

  const lastRow = sheet.getLastRow();
  const newRowRange = sheet.getRange(lastRow, 1, 1, HEADERS.length);
  newRowRange
    .setFontFamily('Manrope')
    .setFontSize(10)
    .setVerticalAlignment('top')
    .setWrap(true);

  sheet.setRowHeight(lastRow, 44);

  // Дата — моно, серым
  sheet.getRange(lastRow, 1).setFontFamily('JetBrains Mono').setFontColor('#666666');
  // Язык — бейдж
  sheet.getRange(lastRow, 2)
    .setFontFamily('JetBrains Mono')
    .setFontWeight('bold')
    .setHorizontalAlignment('center')
    .setBackground('#f0f0f0');
  // FIO — жирным
  sheet.getRange(lastRow, 4).setFontWeight('bold');
  // Телефон — моно
  sheet.getRange(lastRow, 5).setFontFamily('JetBrains Mono').setFontWeight('bold');
}

function jsonResponse_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ============================================================
// TEST
// ============================================================
function testSubmission() {
  const fake = {
    submittedAt: new Date().toISOString(),
    lang: 'UZ',
    event: 'Moliyaviy Savodxonlik · 11-okt-2026',
    name: 'Bekzod Sattorov',
    phone: '+998 90 123 45 67',
    channel: 'Telegram',
    source: 'Instagram',
    pain: 'Nasya toʻlovlari koʻp, oy oxirida pul yetmaydi',
    utm_source: 'instagram',
    utm_medium: 'stories',
    utm_campaign: 'launch',
    referrer: 'https://instagram.com',
    userAgent: 'Test agent'
  };
  appendApplication_(fake);
  Logger.log('✓ Тестовая заявка добавлена. Открой лист "' + SHEET_NAME + '".');
}
