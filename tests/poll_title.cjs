const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../badminton/web/app.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(0, source.indexOf('const tg')), context);
for (const [instant, expected] of [
  ['2026-11-15T11:59:59Z', 'Sunday - 15/11'],
  ['2026-11-15T12:00:00Z', 'Monday - 16/11'],
  ['2026-11-15T23:00:00+07:00', 'Monday - 16/11'],
  ['2026-11-16T00:00:00+07:00', 'Monday - 16/11'],
  ['2026-11-30T19:00:00+07:00', 'Tuesday - 01/12'],
  ['2026-12-31T19:00:00+07:00', 'Friday - 01/01'],
]) {
  assert.equal(context.defaultPollTitle(new Date(instant)), expected, instant);
}
console.log('Poll title timezone, cutoff and date rollover checks passed.');
