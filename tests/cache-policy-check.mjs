import assert from 'node:assert/strict';
import fs from 'node:fs/promises';

const source = await fs.readFile(new URL('../web/sync.js', import.meta.url), 'utf8');
const release = source.indexOf('inFlight.delete(lockId)');
const cleanup = source.indexOf('if (autoCleanAudioKey) await cleanAudioCache', release);

assert.notEqual(release, -1, 'push must release its inFlight guard');
assert.notEqual(cleanup, -1, 'push must schedule automatic cache cleanup');
assert.ok(release < cleanup, 'cache cleanup must happen after inFlight is released');
assert.match(source, /operation\.serverAudioNote && !output\.conflict && !output\.pendingEdits/,
  'automatic cleanup requires an accepted audio push without concurrent edits');
assert.match(source, /async function verifyRemoteAudio\(snapshot\)/,
  'automatic cleanup must recheck the computer before removing its only device cache');
assert.match(source, /await verifyRemoteAudio\(snapshot\)/,
  'cache cleanup must use the real-time computer check');
assert.match(source, /current\.note\.audioAvailable === false \|\| current\.note\.audioDeletedAt/,
  'remote deletion markers must preserve the local cache');
assert.match(source, /entries\.push\(\.\.\.recordEntries\(current\)\)/,
  'cache cleanup must preserve the note record');

console.log('cache policy source checks passed');
