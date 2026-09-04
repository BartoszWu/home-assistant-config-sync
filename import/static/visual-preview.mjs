// Native HA elements are loaded in their own window: real media queries,
// frontend version, theme and user session. No tokens or config-save API here.
export const UNAVAILABLE = 'Visual preview unavailable. YAML diff is still available.';
export const VIEWPORTS = [
  {label: 'Desktop', width: 1440, height: 900},
  {label: 'Mobile', width: 390, height: 844},
];

export function deepFind(root, selector) {
  if (root.shadowRoot) {
    const found = deepFind(root.shadowRoot, selector);
    if (found) return found;
  }
  const direct = root.querySelector(selector);
  if (direct) return direct;
  for (const node of root.querySelectorAll('*')) {
    if (node.shadowRoot) {
      const found = deepFind(node.shadowRoot, selector);
      if (found) return found;
    }
  }
  return null;
}

const deny = () => { throw new Error('Read-only preview'); };
// Native cards may fetch additional data. Unknown commands fail closed.
const READ_COMMANDS = new Set([
  'get_states', 'get_config', 'get_services',
  'history/history_during_period', 'history/stream',
  'recorder/statistics_during_period', 'recorder/get_statistics_metadata',
  'recorder/list_statistic_ids', 'recorder/info', 'energy/get_prefs',
  'weather/subscribe_forecast', 'camera/stream', 'camera/get_prefs',
  'media_player/browse_media', 'media_player/thumbnail',
  'config/entity_registry/list', 'config/device_registry/list',
  'config/area_registry/list', 'config/floor_registry/list',
  'lovelace/config', 'lovelace/resources', 'lovelace/info',
]);

export function readonlyHass(hass) {
  const check = message => {
    if (!READ_COMMANDS.has(message?.type)) deny();
  };
  const connection = new Proxy(hass.connection, {
    get(target, key) {
      if (['sendMessage', 'sendMessagePromise'].includes(key)) {
        return (message, ...args) => { check(message); return target[key](message, ...args); };
      }
      if (key === 'subscribeMessage') {
        return (callback, message, ...args) => {
          check(message);
          return target.subscribeMessage(callback, message, ...args);
        };
      }
      // Do not expose the socket, auth, reconnect or arbitrary connection methods.
      return undefined;
    },
  });
  return {
    ...hass,
    connection,
    auth: undefined,
    callService: deny,
    callApi: (method, ...args) => {
      if (method !== 'GET') deny();
      return hass.callApi(method, ...args);
    },
    callWS: message => { check(message); return hass.callWS(message); },
    sendWS: deny,
  };
}

export function previewLovelace(source, config) {
  // Deliberately do not copy the real dashboard's save/delete/edit callbacks.
  return {
    config: structuredClone(config), rawConfig: structuredClone(config),
    mode: 'yaml', urlPath: source.urlPath, locale: source.locale,
    editMode: false, saveConfig: deny, deleteConfig: deny,
    setEditMode: deny, enableFullEditMode: deny, showToast: () => {},
  };
}

async function untilReady(frame, signal) {
  const deadline = Date.now() + 20000;
  while (!signal.aborted && Date.now() < deadline) {
    // Cross-origin, login, frame policy and missing frontend all fail closed.
    const doc = frame.contentDocument;
    const panel = doc && deepFind(doc, 'ha-panel-lovelace');
    if (panel?.hass && panel.lovelace && panel.shadowRoot?.querySelector('hui-root') &&
        frame.contentWindow.customElements.get('hui-root')) return panel;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(UNAVAILABLE);
}

async function mountNative(frame, config, viewport, signal) {
  const panel = await untilReady(frame, signal);
  if (signal.aborted) throw new Error(UNAVAILABLE);
  const doc = frame.contentDocument;
  const native = doc.createElement('hui-root');
  native.hass = readonlyHass(panel.hass);
  native.lovelace = previewLovelace(panel.lovelace, config);
  native.panel = structuredClone(panel.panel);
  native.route = {prefix: panel.route.prefix, path: '/0'};
  native.narrow = viewport.width < 600;
  native.noEdit = true;
  native.style.display = 'block';
  // Preserve the installed theme without importing or recreating HA styles.
  const theme = frame.contentWindow.getComputedStyle(panel);
  for (const name of theme) {
    if (name.startsWith('--')) native.style.setProperty(name, theme.getPropertyValue(name));
  }
  const original = doc.querySelector('home-assistant');
  if (!original) throw new Error(UNAVAILABLE);
  original.style.display = 'none';
  doc.body.append(native);
  await native.updateComplete;
  // Lit's root completion precedes asynchronous view/card imports.
  const deadline = Date.now() + 15000;
  while (!signal.aborted && Date.now() < deadline) {
    const view = deepFind(native, 'hui-view');
    if (deepFind(native, 'hui-error-card, hass-error-screen')) throw new Error(UNAVAILABLE);
    if (view?.shadowRoot?.childElementCount) {
      await new Promise(resolve => setTimeout(resolve, 500));
      if (signal.aborted || deepFind(native, 'hui-error-card, hass-error-screen')) {
        throw new Error(UNAVAILABLE);
      }
      frame.style.visibility = 'visible';
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(UNAVAILABLE);
}

export async function withTimeout(promise, milliseconds = 35000) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(UNAVAILABLE)), milliseconds);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

export class PreviewSession {
  constructor(container, data, renderer = mountNative) {
    this.container = container;
    this.data = data;
    this.renderer = renderer;
    this.controller = new AbortController();
    this.disposers = [];
  }

  start() {
    // Cache the same promise, including failures, for this review session.
    this.pending ??= this.render();
    return this.pending;
  }

  async render() {
    try {
      if (this.data.error || !/^\/[\w%-]+\/0$/.test(this.data.path)) throw new Error(UNAVAILABLE);
      for (const viewport of VIEWPORTS) {
        const heading = document.createElement('h4');
        heading.textContent = viewport.label;
        const pair = document.createElement('div');
        pair.className = 'preview-pair';
        this.container.append(heading, pair);
        for (const side of ['before', 'after']) {
          if (this.controller.signal.aborted) throw new Error(UNAVAILABLE);
          const cell = document.createElement('div');
          const label = document.createElement('p');
          label.textContent = side === 'before' ? 'Before' : 'After';
          const box = document.createElement('div');
          box.className = 'preview-viewport';
          const frame = document.createElement('iframe');
          frame.title = `${viewport.label} ${label.textContent}`;
          frame.width = viewport.width;
          frame.height = viewport.height;
          frame.tabIndex = -1;
          frame.inert = true;
          frame.style.visibility = 'hidden';
          frame.referrerPolicy = 'same-origin';
          // Only same-origin HA routes prepared by the backend, never a config URL.
          frame.src = this.data.path;
          box.append(frame);
          cell.append(label, box);
          pair.append(cell);
          const resize = new ResizeObserver(() => {
            const scale = Math.min(1, box.clientWidth / viewport.width);
            frame.style.transform = `scale(${scale})`;
            box.style.height = `${viewport.height * scale}px`;
          });
          resize.observe(box);
          this.disposers.push(() => resize.disconnect());
          await withTimeout(this.renderer(frame, this.data[side], viewport, this.controller.signal));
          if (this.controller.signal.aborted) throw new Error(UNAVAILABLE);
        }
      }
    } catch {
      this.dispose();
      throw new Error(UNAVAILABLE);
    }
  }

  dispose() {
    this.controller.abort();
    for (const dispose of this.disposers.splice(0)) dispose();
    // Frames are the only temporary resources. No persisted HA objects exist,
    // even when the browser/process crashes before dispose can execute.
    this.container.replaceChildren();
  }
}

export function installUI(doc = document) {
  for (const review of doc.querySelectorAll('.visual-review')) {
    const visual = review.querySelector('.visual-panel');
    const yaml = review.parentElement.querySelector('.yaml-panel');
    const status = review.querySelector('.preview-status');
    const load = review.querySelector('.preview-load');
    visual.hidden = false;
    yaml.hidden = true;
    for (const tab of review.querySelectorAll('[data-preview-tab]')) {
      tab.setAttribute('aria-selected', String(tab.dataset.previewTab === 'visual'));
    }
    let session;
    load.addEventListener('click', async () => {
      // Bound resources: at most one dashboard (four frames) at a time.
      doc.dispatchEvent(new Event('preview-release'));
      load.hidden = true;
      status.textContent = 'Rendering native Home Assistant preview…';
      let active;
      try {
        active = new PreviewSession(review.querySelector('.preview-renders'),
          JSON.parse(review.querySelector('.preview-data').textContent));
        session = active;
        await active.start();
        if (session === active) status.textContent = 'Read-only preview. No dashboard has been saved.';
      } catch {
        if (!active || session === active) status.textContent = UNAVAILABLE;
      }
    });
    doc.addEventListener('preview-release', () => {
      if (session) {
        session.dispose();
        session = null;
        load.hidden = false;
        status.textContent = 'Preview closed.';
      }
    });
    for (const tab of review.querySelectorAll('[data-preview-tab]')) {
      tab.addEventListener('click', () => {
        const showVisual = tab.dataset.previewTab === 'visual';
        visual.hidden = !showVisual;
        yaml.hidden = showVisual;
        for (const button of review.querySelectorAll('[data-preview-tab]')) {
          button.setAttribute('aria-selected', String(button === tab));
        }
      });
    }
  }
  const disposeAll = () => doc.dispatchEvent(new Event('preview-release'));
  doc.getElementById('apply-form')?.addEventListener('reset', disposeAll);
  doc.getElementById('apply-form')?.addEventListener('submit', () => {
    if (doc.getElementById('apply-form').dataset.submitting === 'true') disposeAll();
  });
  window.addEventListener('pagehide', disposeAll);
}

if (typeof document !== 'undefined') installUI();
