import { LitElement, css, html } from "lit";

import { FRAME_LOAD_TIMEOUT_MS, frameMode } from "./logic.mjs";

/**
 * Admin sidebar panel: one tab per Kiosk Satellite, embedding its admin page.
 */
class KioskMgmtPanel extends LitElement {
  static properties = {
    hass: { attribute: false },
    narrow: { type: Boolean },
    panel: { attribute: false },
    _kiosks: { state: true },
    _selected: { state: true },
    _frame: { state: true },
    _attempt: { state: true },
  };

  constructor() {
    super();
    this._kiosks = [];
    this._selected = null;
    this._frame = "pending";
    this._attempt = 0;
    this._unsub = null;
    this._pending = false;
    this._timer = 0;
  }

  connectedCallback() {
    super.connectedCallback();
    this._subscribe();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._clearTimer();
    if (this._unsub) {
      this._unsub();
      this._unsub = null;
    }
  }

  updated(changed) {
    if (changed.has("hass")) {
      this._subscribe();
    }
    if (changed.has("_selected") || changed.has("_attempt") || changed.has("_kiosks")) {
      this._armFrame();
    }
  }

  async _subscribe() {
    if (!this.hass || this._unsub || this._pending) {
      return;
    }
    this._pending = true;
    // subscribeMessage resolves to the unsubscribe function.
    this._unsub = await this.hass.connection.subscribeMessage(
      (event) => {
        this._kiosks = event.kiosks || [];
        if (!this._kiosks.some((kiosk) => kiosk.id === this._selected)) {
          this._selected = this._kiosks[0] ? this._kiosks[0].id : null;
          this._frame = "pending";
        }
      },
      { type: "kiosk_mgmt/subscribe" },
    );
  }

  get _kiosk() {
    return this._kiosks.find((kiosk) => kiosk.id === this._selected) || null;
  }

  _select(id) {
    if (id === this._selected) {
      return;
    }
    this._selected = id;
    this._frame = "pending";
    this._attempt += 1;
  }

  _armFrame() {
    this._clearTimer();
    const kiosk = this._kiosk;
    if (!kiosk) {
      return;
    }
    const mode = frameMode(window.location.protocol, kiosk, this._frame);
    if (mode !== "iframe" || kiosk.scheme !== "https") {
      return;
    }
    this._timer = window.setTimeout(() => {
      if (this._frame !== "loaded") {
        this._frame = "failed";
      }
    }, FRAME_LOAD_TIMEOUT_MS);
  }

  _clearTimer() {
    if (this._timer) {
      window.clearTimeout(this._timer);
      this._timer = 0;
    }
  }

  _onLoad() {
    this._clearTimer();
    this._frame = "loaded";
  }

  _onError() {
    this._clearTimer();
    this._frame = "failed";
  }

  _retry() {
    this._frame = "pending";
    this._attempt += 1;
  }

  async _refresh() {
    await this.hass.callService("kiosk_mgmt", "refresh");
  }

  render() {
    const kiosk = this._kiosk;
    return html`
      <div class="wrap">
        <header>
          <h1>Kiosk Mgmt</h1>
          <button class="quiet" @click=${this._refresh}>Refresh</button>
        </header>
        ${this._kiosks.length
          ? html`
              <div class="tabs" role="tablist">
                ${this._kiosks.map(
                  (item) => html`
                    <button
                      role="tab"
                      class=${item.id === this._selected ? "tab on" : "tab"}
                      aria-selected=${item.id === this._selected}
                      @click=${() => this._select(item.id)}
                    >
                      <span class="dot ${item.online ? "on" : ""}"></span>
                      ${item.name}
                    </button>
                  `,
                )}
              </div>
              ${kiosk ? this._body(kiosk) : ""}
            `
          : html`<p class="empty">No Kiosk Satellite tablets found yet.</p>`}
      </div>
    `;
  }

  _body(kiosk) {
    const mode = frameMode(window.location.protocol, kiosk, this._frame);
    const seen = kiosk.last_seen
      ? new Date(kiosk.last_seen).toLocaleString()
      : "Not yet";
    return html`
      <section class="info">
        <div><span>Name</span><strong>${kiosk.name}</strong></div>
        <div><span>IP</span><strong>${kiosk.host}</strong></div>
        <div><span>Model</span><strong>${kiosk.model || "Unknown"}</strong></div>
        <div>
          <span>Status</span>
          <strong>${kiosk.online ? "Online" : "Offline"}</strong>
        </div>
        <div><span>Last seen</span><strong>${seen}</strong></div>
        <a href=${kiosk.url} target="_blank" rel="noopener noreferrer">Open in new tab</a>
      </section>
      ${mode === "mixed"
        ? html`
            <div class="card">
              <h2>Can't embed this page</h2>
              <p>
                Home Assistant is open over HTTPS, and this kiosk's admin page is
                plain HTTP. Browsers block that mix, so the page can't be shown
                here. Open it in a new tab instead.
              </p>
              <a class="button" href=${kiosk.url} target="_blank" rel="noopener noreferrer"
                >Open in new tab</a
              >
            </div>
          `
        : mode === "trust"
          ? html`
              <div class="card">
                <h2>Trust certificate</h2>
                <p>
                  This kiosk's admin page uses HTTPS with a certificate this
                  browser does not trust yet. Kiosk Satellite creates a
                  self-signed certificate unless you imported one. Open the page
                  in a new tab, accept the certificate once, then choose Retry.
                </p>
                <div class="actions">
                  <a class="button" href=${kiosk.url} target="_blank" rel="noopener noreferrer"
                    >Open in new tab</a
                  >
                  <button class="button" @click=${this._retry}>Retry</button>
                </div>
              </div>
            `
          : html`
              <iframe
                title=${`Admin for ${kiosk.name}`}
                src=${`${kiosk.url}${kiosk.url.includes("?") ? "&" : "?"}km=${this._attempt}`}
                @load=${this._onLoad}
                @error=${this._onError}
              ></iframe>
            `}
    `;
  }

  static styles = css`
    :host {
      display: block;
      height: 100%;
      background: var(--primary-background-color, #111);
      color: var(--primary-text-color, #e8e8e8);
    }
    .wrap {
      display: flex;
      flex-direction: column;
      height: 100%;
      box-sizing: border-box;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px 0;
    }
    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 500;
    }
    .quiet,
    .button {
      background: var(--primary-color, #03a9f4);
      color: var(--text-primary-color, #fff);
      border: 0;
      border-radius: 8px;
      padding: 8px 14px;
      text-decoration: none;
      cursor: pointer;
      font: inherit;
    }
    .tabs {
      display: flex;
      gap: 8px;
      padding: 12px 16px;
      overflow-x: auto;
    }
    .tab {
      display: flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--divider-color, #333);
      background: var(--card-background-color, #1c1c1c);
      color: inherit;
      border-radius: 999px;
      padding: 6px 12px;
      cursor: pointer;
      font: inherit;
    }
    .tab.on {
      border-color: var(--primary-color, #03a9f4);
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #888;
    }
    .dot.on {
      background: var(--success-color, #4caf50);
    }
    .info {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      align-items: end;
      padding: 0 16px 12px;
    }
    .info span {
      display: block;
      font-size: 12px;
      opacity: 0.7;
    }
    .info a {
      color: var(--primary-color, #03a9f4);
    }
    iframe {
      flex: 1;
      width: 100%;
      border: 0;
      background: #fff;
    }
    .card,
    .empty {
      margin: 8px 16px;
      padding: 20px;
      border-radius: 12px;
      background: var(--card-background-color, #1c1c1c);
    }
    .card h2 {
      margin: 0 0 8px;
      font-size: 18px;
    }
    .actions {
      display: flex;
      gap: 8px;
    }
  `;
}

customElements.define("kiosk-mgmt-panel", KioskMgmtPanel);
