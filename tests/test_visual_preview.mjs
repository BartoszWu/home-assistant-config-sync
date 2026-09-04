import {test} from 'node:test';
import assert from 'node:assert/strict';
import {PreviewSession, readonlyHass, previewLovelace, UNAVAILABLE, withTimeout, deepFind} from '../import/static/visual-preview.mjs';

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren() { this.children = []; }
}
let connected = 0;
globalThis.document = {createElement: tag => new Element(tag)};
globalThis.ResizeObserver = class {
  observe() { connected++; }
  disconnect() { connected--; }
};
const data = {path: '/test-dashboard/0', before: {views: [{title: 'Before'}]}, after: {views: [{title: 'After'}]}};

test('finds the root component own shadow DOM', () => {
  const view = {};
  assert.equal(deepFind({shadowRoot: {querySelector: () => view}}, 'hui-view'), view);
});

test('hung frontend update times out without leaking its error', async () => {
  await assert.rejects(withTimeout(new Promise(() => {}), 5), {message: UNAVAILABLE});
});

test('four native renders use correct config and viewport, cached per session', async () => {
  const container = new Element('div');
  const calls = [];
  const session = new PreviewSession(container, data, async (frame, config, viewport) => {
    calls.push([frame.width, frame.height, config.views[0].title]);
    assert.equal(frame.inert, true);
    assert.equal(frame.src, '/test-dashboard/0');
  });
  const first = session.start();
  assert.equal(session.start(), first);
  await first;
  assert.deepEqual(calls, [[1440, 900, 'Before'], [1440, 900, 'After'], [390, 844, 'Before'], [390, 844, 'After']]);
  session.dispose();
  assert.equal(container.children.length, 0);
  assert.equal(connected, 0);
});

test('error cleans all frames and observers, not just the failed frame', async () => {
  const container = new Element('div');
  let calls = 0;
  const session = new PreviewSession(container, data, async () => {
    if (++calls === 3) throw new Error('must not expose internal errors');
  });
  await assert.rejects(session.start(), {message: UNAVAILABLE});
  assert.equal(container.children.length, 0);
  assert.equal(connected, 0);
  await assert.rejects(session.start());
  assert.equal(calls, 3);
});

test('cancel during rendering aborts and cleans temporary frames', async () => {
  const container = new Element('div');
  let proceed;
  const session = new PreviewSession(container, data, () => new Promise(resolve => { proceed = resolve; }));
  const pending = session.start();
  session.dispose();
  proceed();
  await assert.rejects(pending);
  assert.equal(container.children.length, 0);
  assert.equal(connected, 0);
});

test('invalid preview never creates a frame', async () => {
  const container = new Element('div');
  for (const value of [{error: UNAVAILABLE}, {...data, path: '//untrusted.example/0'}]) {
    const session = new PreviewSession(container, value, () => assert.fail('renderer called'));
    await assert.rejects(session.start());
    assert.equal(container.children.length, 0);
  }
});

test('preview config has no production mutation callbacks or shared config objects', () => {
  const original = structuredClone(data.after);
  const source = {urlPath: 'test-dashboard', saveConfig: () => assert.fail('production save')};
  const preview = previewLovelace(source, original);
  assert.throws(() => preview.saveConfig({}), /Read-only/);
  assert.throws(() => preview.deleteConfig(), /Read-only/);
  assert.throws(() => preview.setEditMode(true), /Read-only/);
  preview.config.views[0].title = 'Changed';
  assert.deepEqual(original, data.after);
});

test('native card API facade blocks mutations and hides socket/auth', () => {
  const messages = [];
  const send = message => messages.push(message);
  const readonly = readonlyHass({
    auth: {}, connection: {sendMessagePromise: send, socket: {}},
    callWS: send, callService: () => assert.fail('service call'),
  });
  readonly.callWS({type: 'get_states'});
  assert.equal(messages.length, 1);
  assert.throws(() => readonly.callWS({type: 'lovelace/config/save'}));
  assert.throws(() => readonly.connection.sendMessagePromise({type: 'lovelace/dashboards/create'}));
  assert.throws(() => readonly.callService('light', 'turn_on'));
  assert.throws(() => readonly.callApi('POST', 'anything'));
  assert.equal(readonly.connection.socket, undefined);
  assert.equal(readonly.auth, undefined);
  assert.equal(messages.length, 1);
});
