import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('./hooks/register.js', import.meta.url), 'utf8');
const { checksum, formatFrame } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const fixtures = [
  'S2:test:4:1/1 t=1791055975;l=30;p=fail;m=idle;c=bad;r=auth;h=972f8d05',
  'S2:test:4:1/3 t=1791055975;l=30;p=fa\nS2:test:4:2/3 il;m=idle;c=bad;r=auth\nS2:test:4:3/3 ;h=972f8d05',
  'S2:test:4:1/1 t=1791055975;l=30;p=fail;m=idle;c=bad;r=auth;h=972f8d05',
];
const payload = 't=1791055975;l=30;p=fail;m=idle;c=bad;r=auth';
for (const fixture of fixtures) {
  const parts = fixture.split('\n').map(row => /^S2:test:4:(\d+)\/(\d+) ([A-Za-z0-9=;_.-]+)$/.exec(row));
  assert(parts.every(Boolean));
  parts.forEach((part, index) => { assert.equal(Number(part[1]), index + 1); assert.equal(Number(part[2]), parts.length); });
  const joined = parts.map(part => part[3]).join('');
  assert.equal(joined, payload + ';h=972f8d05');
  assert.equal(checksum(payload), '972f8d05');
}
let cases = 0;
for (const width of [16, 20, 24, 40, 60, 80, 120]) {
  for (const height of [0, 1, 2, 4, 6, 8, 20]) {
    for (const revision of [1, 184, 999999999]) {
      const rows = formatFrame(payload, 'test', revision, width, height);
      if (rows.length) {
        assert(rows.length + 1 <= Math.min(8, height));
        assert(rows.every(row => row.length < width));
        assert.equal(rows.map(row => row.slice(row.indexOf(' ') + 1)).join(''), payload + ';h=' + checksum(payload));
      }
      cases++;
    }
  }
}
console.log(`PASS: ${fixtures.length} real copied captures reconstruct identically; checksum 972f8d05 verified.`);
console.log(`PASS: ${cases} formatter width/row/sequence combinations.`);
console.log('Not a production parser test: registration, source lease and live Desktop extraction remain separate.');
