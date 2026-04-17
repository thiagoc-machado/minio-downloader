// ==UserScript==
// @name         Minno API capture bridge
// @namespace    local.codex.capture
// @version      1.0.0
// @description  Captura as respostas de roll e play-options e envia para o backend local.
// @match        https://kids.gominno.com/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @connect      localhost
// @connect      127.0.0.1
// @connect      192.168.1.230
// @connect      ::1
// @connect      __CAPTURE_CONNECT__
// ==/UserScript==

(function () {
  'use strict';

  const ENDPOINT_PLACEHOLDER = '__CAPTURE_ENDPOINT__';
  const DEFAULT_ENDPOINT = 'http://192.168.1.230:8090/api/capture';
  const ENDPOINT_STORAGE_KEY = 'tm_minno_capture_endpoint';
  const STORAGE_KEY = 'tm_minno_capture_panel_state';
  const CAPTURE_SESSION = `sess_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  const SENT_SIGNATURES = new Set();
  const STATE = {
    active: false,
    completed: false,
    error: '',
    lastMessage: 'Pronto para iniciar.',
    rollReady: false,
    detailsReady: false,
    rollSent: false,
    detailsSent: false,
  };

  let panel;
  let statusText;
  let startButton;
  let endpointHint;

  function resolveEndpoint() {
    try {
      const saved = localStorage.getItem(ENDPOINT_STORAGE_KEY);
      if (saved && /^https?:\/\//i.test(saved)) {
        return saved.replace(/\/+$/, '');
      }
    } catch (_) {}
    if (ENDPOINT_PLACEHOLDER !== '__CAPTURE_ENDPOINT__' && /^https?:\/\//i.test(ENDPOINT_PLACEHOLDER)) {
      return ENDPOINT_PLACEHOLDER.replace(/\/+$/, '');
    }
    return DEFAULT_ENDPOINT;
  }

  function loadState() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (saved && typeof saved === 'object') {
        Object.assign(STATE, saved);
      }
    } catch (_) {}
  }

  function saveState() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        active: STATE.active,
        completed: STATE.completed,
        error: STATE.error,
        lastMessage: STATE.lastMessage,
        rollReady: STATE.rollReady,
        detailsReady: STATE.detailsReady,
        rollSent: STATE.rollSent,
        detailsSent: STATE.detailsSent,
      }));
    } catch (_) {}
  }

  function setStatus(message, kind) {
    STATE.lastMessage = message;
    STATE.error = kind === 'error' ? message : '';
    if (statusText) {
      statusText.textContent = message;
      statusText.dataset.kind = kind || 'info';
    }
    updateButton();
    saveState();
  }

  function updateButton() {
    if (!startButton) return;

    const done = STATE.rollSent && STATE.detailsSent;
    const partial = STATE.rollReady || STATE.detailsReady;
    const active = STATE.active && !done;

    startButton.classList.remove('is-idle', 'is-active', 'is-partial', 'is-done', 'is-error');
    if (STATE.error) {
      startButton.classList.add('is-error');
      startButton.textContent = 'Erro no envio';
    } else if (done || STATE.completed) {
      startButton.classList.add('is-done');
      startButton.textContent = 'Dados enviados';
    } else if (active) {
      startButton.classList.add(partial ? 'is-partial' : 'is-active');
      startButton.textContent = partial ? 'Capturando...' : 'Monitorando...';
    } else {
      startButton.classList.add('is-idle');
      startButton.textContent = 'Iniciar monitoramento';
    }
  }

  function createPanel() {
    if (panel) return;

    panel = document.createElement('div');
    panel.id = 'tm-minno-capture-panel';
    panel.innerHTML = `
      <button type="button" id="tm-minno-capture-btn">Iniciar monitoramento</button>
      <div id="tm-minno-capture-status">Pronto para iniciar.</div>
      <div id="tm-minno-capture-endpoint"></div>
      <div id="tm-minno-capture-hint">Ative antes de abrir o player para capturar a primeira chamada.</div>
    `;
    document.documentElement.appendChild(panel);

    startButton = panel.querySelector('#tm-minno-capture-btn');
    statusText = panel.querySelector('#tm-minno-capture-status');
    endpointHint = panel.querySelector('#tm-minno-capture-endpoint');
    startButton.addEventListener('click', toggleMonitoring);
    updateButton();
    setStatus(STATE.lastMessage || 'Pronto para iniciar.');
    updateEndpointHint();
  }

  function injectStyles() {
    if (document.getElementById('tm-minno-capture-style')) return;
    const style = document.createElement('style');
    style.id = 'tm-minno-capture-style';
    style.textContent = `
      #tm-minno-capture-panel {
        position: fixed;
        right: 18px;
        bottom: 18px;
        z-index: 2147483647;
        width: 240px;
        padding: 12px;
        border-radius: 18px;
        background: rgba(7, 14, 28, 0.94);
        border: 1px solid rgba(148, 163, 184, 0.22);
        box-shadow: 0 18px 50px rgba(0, 0, 0, 0.38);
        color: #e6f0ff;
        font: 13px/1.45 Arial, Helvetica, sans-serif;
        backdrop-filter: blur(12px);
      }
      #tm-minno-capture-panel button {
        width: 100%;
        padding: 10px 12px;
        border: 0;
        border-radius: 12px;
        color: #031522;
        background: linear-gradient(135deg, #8be8ff, #48d6ff 48%, #6fffb9);
        font-weight: 700;
        cursor: pointer;
      }
      #tm-minno-capture-panel button.is-idle {
        background: rgba(255, 255, 255, 0.08);
        color: #e6f0ff;
        border: 1px solid rgba(148, 163, 184, 0.2);
      }
      #tm-minno-capture-panel button.is-active {
        background: linear-gradient(135deg, #4dd4ff, #21c78d);
        color: #031522;
      }
      #tm-minno-capture-panel button.is-partial {
        background: linear-gradient(135deg, #f59e0b, #fb7185);
        color: #1c1200;
      }
      #tm-minno-capture-panel button.is-done {
        background: linear-gradient(135deg, #21c78d, #6fffb9);
        color: #031522;
      }
      #tm-minno-capture-panel button.is-error {
        background: linear-gradient(135deg, #fb7185, #ef4444);
        color: #fff;
      }
      #tm-minno-capture-status {
        margin-top: 10px;
        font-size: 12px;
        color: #cfe0f6;
        word-break: break-word;
      }
      #tm-minno-capture-status[data-kind="error"] {
        color: #ffb5c0;
      }
      #tm-minno-capture-endpoint {
        margin-top: 8px;
        color: #7ee1ff;
        font-size: 11px;
        line-height: 1.35;
        word-break: break-word;
      }
      #tm-minno-capture-hint {
        margin-top: 8px;
        color: #91a4bf;
        font-size: 11px;
        line-height: 1.4;
      }
    `;
    document.documentElement.appendChild(style);
  }

  function isJsonLike(response) {
    try {
      const contentType = response && response.headers && response.headers.get ? response.headers.get('content-type') || '' : '';
      return contentType.includes('application/json') || contentType.includes('+json');
    } catch (_) {
      return false;
    }
  }

  function normalizeUrl(input) {
    if (!input) return '';
    if (typeof input === 'string') return input;
    if (input && typeof input.url === 'string') return input.url;
    return '';
  }

  function classifyUrl(url) {
    const value = String(url || '');
    if (/\/v1\/client\/roll(?:\?|$)/i.test(value)) return 'manifest';
    if (/\/play-options(?:\?|$)/i.test(value)) return 'details';
    return '';
  }

  function deriveCaptureKey(payload, fallbackUrl) {
    const root = payload && typeof payload === 'object' && payload.response && typeof payload.response === 'object'
      ? payload.response
      : payload;

    if (root && typeof root === 'object') {
      const manifestUri = root.manifest_uri || root.manifestUrl || root.manifest_url || root.playback_uri || root.playbackUrl || root.playback_url || root.stream_uri || root.streamUrl || root.stream_url || root.hls_url || root.hlsUrl || root.url;
      if (typeof manifestUri === 'string' && manifestUri.trim()) {
        return manifestUri.trim().split('?', 1)[0].split('#', 1)[0];
      }

      const programId = root.ProgramId || root.programId;
      if (typeof programId === 'string' && programId.trim()) {
        return programId.trim();
      }

      const vods = root.Vods || root.vods;
      if (Array.isArray(vods) && vods.length > 0) {
        const firstVod = vods[0] && typeof vods[0] === 'object' ? vods[0] : {};
        const catalog = firstVod.CatalogInfo && typeof firstVod.CatalogInfo === 'object' ? firstVod.CatalogInfo : {};
        if (typeof catalog.SeriesId === 'string' && catalog.SeriesId.trim()) return catalog.SeriesId.trim();
        if (typeof catalog.EpisodeId === 'string' && catalog.EpisodeId.trim()) return catalog.EpisodeId.trim();
        if (typeof catalog.ProgramId === 'string' && catalog.ProgramId.trim()) return catalog.ProgramId.trim();
        if (typeof firstVod.Id === 'string' && firstVod.Id.trim()) return firstVod.Id.trim();
      }
    }

    return String(fallbackUrl || '').split('?', 1)[0];
  }

  function postPayload(payload) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url: resolveEndpoint(),
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        data: JSON.stringify(payload),
        onload: (res) => {
          const text = res && typeof res.responseText === 'string' ? res.responseText : '';
          let data = null;
          try {
            data = text ? JSON.parse(text) : null;
          } catch (_) {}
          if (res.status >= 200 && res.status < 300) {
            resolve(data || { ok: true });
          } else {
            reject(new Error((data && (data.error || data.message)) || text || `HTTP ${res.status}`));
          }
        },
        onerror: () => reject(new Error('Falha ao enviar para o backend.')),
        ontimeout: () => reject(new Error('Timeout ao enviar para o backend.')),
      });
    });
  }

  function updateEndpointHint() {
    if (!endpointHint) return;
    const endpoint = resolveEndpoint();
    const isFallback = endpoint === DEFAULT_ENDPOINT;
    endpointHint.textContent = isFallback
      ? `Backend: ${endpoint} (fallback local)`
      : `Backend: ${endpoint}`;
  }

  async function handleCapturedResponse(type, url, payload) {
    if (!STATE.active || STATE.completed) return;
    const signature = `${type}|${url}`;
    if (SENT_SIGNATURES.has(signature)) return;
    SENT_SIGNATURES.add(signature);

    const captureKey = deriveCaptureKey(payload, url);
    const body = {
      capture_type: type,
      capture_session: CAPTURE_SESSION,
      capture_key: captureKey,
      source_url: url,
      payload,
    };

    try {
      const result = await postPayload(body);
      if (result && result.ignored) {
        setStatus(`Ignorado pelo backend: ${result.reason || 'filtro de captura'}`, 'error');
        return;
      }

      if (type === 'manifest') {
        STATE.rollReady = true;
        STATE.rollSent = true;
      } else if (type === 'details') {
        STATE.detailsReady = true;
        STATE.detailsSent = true;
      }

      const done = STATE.rollSent && STATE.detailsSent;
      if (done) {
        STATE.completed = true;
        STATE.active = false;
        setStatus('Roll e play-options enviados com sucesso.');
      } else {
        setStatus(type === 'manifest' ? 'Roll capturado e enviado.' : 'Play-options capturado e enviado.');
      }
    } catch (err) {
      SENT_SIGNATURES.delete(signature);
      setStatus(err && err.message ? err.message : 'Erro no envio.', 'error');
    }
  }

  function tryHandleText(url, text, type) {
    if (!text) return;
    let payload = null;
    try {
      payload = JSON.parse(text);
    } catch (_) {
      return;
    }
    handleCapturedResponse(type, url, payload);
  }

  function installFetchHook() {
    if (window.__tmMinnoFetchHooked) return;
    window.__tmMinnoFetchHooked = true;
    const originalFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      const url = normalizeUrl(input);
      return originalFetch(input, init).then((response) => {
        const captureType = classifyUrl(url);
        if (STATE.active && captureType && response && response.ok && isJsonLike(response)) {
          response.clone().text().then((text) => tryHandleText(url, text, captureType)).catch(() => {});
        }
        return response;
      });
    };
  }

  function installXhrHook() {
    if (window.__tmMinnoXhrHooked) return;
    window.__tmMinnoXhrHooked = true;
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function (method, url) {
      this.__tmCaptureMethod = method;
      this.__tmCaptureUrl = url;
      return originalOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function () {
      this.addEventListener('loadend', function () {
        const url = normalizeUrl(this.__tmCaptureUrl);
        const captureType = classifyUrl(url);
        if (!STATE.active || !captureType) return;
        if (this.status < 200 || this.status >= 300) return;
        const text = typeof this.responseText === 'string' ? this.responseText : '';
        tryHandleText(url, text, captureType);
      });
      return originalSend.apply(this, arguments);
    };
  }

  function startMonitoring() {
    if (STATE.completed) {
      STATE.completed = false;
    }
    STATE.active = true;
    STATE.error = '';
    setStatus('Monitoramento ativo. Aguarde as chamadas da página.');
  }

  function stopMonitoring(message) {
    STATE.active = false;
    setStatus(message || 'Monitoramento pausado.');
  }

  function toggleMonitoring() {
    if (STATE.active) {
      stopMonitoring('Monitoramento pausado.');
      return;
    }

    SENT_SIGNATURES.clear();
    STATE.rollReady = false;
    STATE.detailsReady = false;
    STATE.rollSent = false;
    STATE.detailsSent = false;
    STATE.completed = false;
    STATE.error = '';
    startMonitoring();
  }

  function boot() {
    loadState();
    injectStyles();
    createPanel();
    updateEndpointHint();
    if (STATE.active && !STATE.completed) {
      setStatus(STATE.lastMessage || 'Monitoramento ativo.');
    } else if (STATE.completed) {
      setStatus('Dados já enviados nesta sessão.');
    } else {
      updateButton();
      if (resolveEndpoint() === DEFAULT_ENDPOINT) {
        setStatus('Usando fallback em 192.168.1.230:8090. Se seu backend estiver em outro host, baixe o userscript pela aplicação.', 'error');
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }

  installFetchHook();
  installXhrHook();
})();
