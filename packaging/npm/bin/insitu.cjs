#!/usr/bin/env node
'use strict';
// No postinstall hook, network download, shell interpolation, or build fallback.
const { spawn } = require('node:child_process');
const path = require('node:path');

function packageFor(platform, arch, libc = 'gnu') {
  const supported = {
    'linux-x64-gnu': 'linux-x64-gnu',
    'linux-arm64-gnu': 'linux-arm64-gnu',
    'darwin-x64': 'darwin-x64',
    'darwin-arm64': 'darwin-arm64',
    'win32-x64': 'win32-x64-msvc'
  };
  const key = platform === 'linux' ? `${platform}-${arch}-${libc}` : `${platform}-${arch}`;
  if (!supported[key]) {
    throw new Error(`Unsupported platform ${key}. Build Insitu with Cargo on this platform; no binary will be downloaded automatically.`);
  }
  return `@marcusinthesky/insitu-${supported[key]}`;
}

function resolveBinary() {
  if (process.env.INSITU_BINARY_PATH) return path.resolve(process.env.INSITU_BINARY_PATH);
  const libc = process.platform === 'linux' && !process.report?.getReport()?.header?.glibcVersionRuntime ? 'musl' : 'gnu';
  const name = packageFor(process.platform, process.arch, libc);
  const executable = process.platform === 'win32' ? 'insitu.exe' : 'insitu';
  try {
    return require.resolve(`${name}/bin/${executable}`);
  } catch (cause) {
    throw new Error(`Native package ${name} is missing. Reinstall without --omit=optional, or set INSITU_BINARY_PATH to a trusted executable.`, { cause });
  }
}

function main() {
  let executable;
  try { executable = resolveBinary(); }
  catch (error) { console.error(`insitu: ${error.message}`); process.exitCode = 2; return; }
  const child = spawn(executable, process.argv.slice(2), { stdio: 'inherit', env: process.env, windowsHide: true });
  const handlers = new Map();
  for (const signal of ['SIGINT', 'SIGTERM']) {
    const handler = () => { if (!child.killed) child.kill(signal); };
    handlers.set(signal, handler);
    process.on(signal, handler);
  }
  function cleanup() { for (const [signal, handler] of handlers) process.removeListener(signal, handler); }
  child.once('error', error => { cleanup(); console.error(`insitu: cannot start native executable: ${error.message}`); process.exitCode = 2; });
  child.once('exit', (code, signal) => {
    cleanup();
    if (signal) {
      try { process.kill(process.pid, signal); }
      catch { process.exitCode = signal === 'SIGINT' ? 130 : 143; }
    } else { process.exitCode = code ?? 2; }
  });
}
module.exports = { packageFor, resolveBinary, main };
if (require.main === module) main();
