'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const { packageFor } = require('../../packaging/npm/bin/insitu.cjs');
const launcher = path.resolve(__dirname, '../../packaging/npm/bin/insitu.cjs');

test('select supported native packages', () => {
  assert.equal(packageFor('linux', 'x64'), '@marcusinthesky/insitu-linux-x64-gnu');
  assert.equal(packageFor('linux', 'arm64'), '@marcusinthesky/insitu-linux-arm64-gnu');
  assert.equal(packageFor('darwin', 'arm64'), '@marcusinthesky/insitu-darwin-arm64');
  assert.equal(packageFor('darwin', 'x64'), '@marcusinthesky/insitu-darwin-x64');
  assert.equal(packageFor('win32', 'x64'), '@marcusinthesky/insitu-win32-x64-msvc');
});
test('unsupported platform fails explicitly', () => {
  assert.throws(() => packageFor('freebsd', 'x64'), /Unsupported platform/);
  assert.throws(() => packageFor('linux', 'x64', 'musl'), /Unsupported platform/);
  assert.throws(() => packageFor('win32', 'arm64'), /Unsupported platform/);
});
test('forwards argument boundaries without shell interpretation', () => {
  // Node is deliberately a stand-in executable: this tests only the wrapper.
  const result = spawnSync(process.execPath, [launcher, '-e', 'console.log(JSON.stringify(process.argv.slice(1)))', '--', 'space value', '$(echo forbidden)', '🦀'], {
    env: { ...process.env, INSITU_BINARY_PATH: process.execPath }, encoding: 'utf8'
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), ['space value', '$(echo forbidden)', '🦀']);
});
for (const code of [0, 1, 2, 17]) {
  test(`preserves child exit code ${code}`, () => {
    const result = spawnSync(process.execPath, [launcher, '-e', `process.exit(${code})`], {
      env: { ...process.env, INSITU_BINARY_PATH: process.execPath }, encoding: 'utf8'
    });
    assert.equal(result.status, code, result.stderr);
  });
}
test('missing binary produces a clear error', () => {
  const result = spawnSync(process.execPath, [launcher], {
    env: { ...process.env, INSITU_BINARY_PATH: path.join(__dirname, 'does-not-exist') }, encoding: 'utf8'
  });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /cannot start native executable/);
});
