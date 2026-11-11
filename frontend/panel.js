import { LitElement, css, html } from "lit";

import { FRAME_LOAD_TIMEOUT_MS, PROXY_SOURCE, frameMode } from "./logic.mjs";

/**
 * Admin sidebar panel: one tab per Kiosk Satellite.
 *
 * The iframe loads a signed Home Assistant proxy URL, not the tablet.
 * The signed URL lasts a few minutes. This element refreshes it, and the
 * cookie it sets, without navigating the iframe away from the page the
 * user is on. If the frame never reports that it loaded, the tab shows a
 * message, a retry, and a direct link to the tablet.
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
    _iframeUrl: { state: true },
    _openUrl: { state: true },
  };

  constructor() {
    super();
    this._kiosks = [];
    this._selected = null;
    this._frame = "pending";
    this._attempt = 0;
    this._iframeUrl = null;
    this._openUrl = null;
    this._unsub = null;
    this._pending = false;
    this._timer = 0;
    this._renewTimer = 0;
    this._signGen = 0;
    this._onMessage = (event) => this._handleMessage(event);
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener("message", this._onMessage);
    this._subscribe();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener("message", this._onMessage);
    this._clearTimers();
    if (this._unsub) {
      this._unsub();
      this._unsub = null;
    }
  }

  updated(changed) {
    if (changed.has("hass")) {
      this._subscribe();
    }
    if (changed.has("_selected") || changed.has("_attempt")) {
      this._sign(true);
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
    this._iframeUrl = null;
    this._attempt += 1;
  }

  async _sign(navigate) {
    const kiosk = this._kiosk;
    const generation = ++this._signGen;
    this._clearTimers();
    if (!kiosk || !this.hass) {
      this._iframeUrl = null;
      this._openUrl = null;
      return;
    }
    try {
      const result = await this.hass.connection.sendMessagePromise({
        type: "kiosk_mgmt/sign_proxy",
        kiosk_id: kiosk.id,
      });
      if (generation !== this._signGen) {
        return;
      }
      this._openUrl = result.url;
      if (navigate) {
        this._iframeUrl = result.url;
        this._frame = "pending";
        this._armLoadTimer();
      }
      this._armRenew(result.expires_in);
    } catch (_err) {
      if (generation !== this._signGen) {
        return;
      }
      this._iframeUrl = null;
      this._openUrl = null;
      this._frame = "failed";
    }
  }

  _armLoadTimer() {
    this._timer = window.setTimeout(() => {
      if (this._frame !== "loaded") {
        this._frame = "failed";
      }
    }, FRAME_LOAD_TIMEOUT_MS);
  }

  _armRenew(expiresIn) {
    const seconds = Number(expiresIn) || 300;
    const delay = Math.max(15, seconds - 60) * 1000;
    this._renewTimer = window.setTimeout(() => this._renew(), delay);
  }

  async _renew() {
    const kiosk = this._kiosk;
    if (!kiosk || !this.hass) {
      return;
    }
    try {
      const result = await this.hass.connection.sendMessagePromise({
        type: "kiosk_mgmt/sign_proxy",
        kiosk_id: kiosk.id,
      });
      this._openUrl = result.url;
      // Refresh the cookie. redirect:manual still stores Set-Cookie and
      // does not replace the document the iframe is showing.
      await fetch(result.url, {
        credentials: "same-origin",
        redirect: "manual",
        cache: "no-store",
      });
      this._armRenew(result.expires_in);
    } catch (_err) {
      this._armRenew(60);
    }
  }

  _clearTimers() {
    if (this._timer) {
      window.clearTimeout(this._timer);
      this._timer = 0;
    }
    if (this._renewTimer) {
      window.clearTimeout(this._renewTimer);
      this._renewTimer = 0;
    }
  }

  _handleMessage(event) {
    if (event.origin !== window.location.origin) {
      return;
    }
    const data = event.data;
    if (!data || data.source !== PROXY_SOURCE) {
      return;
    }
    if (data.ok) {
      if (this._timer) {
        window.clearTimeout(this._timer);
        this._timer = 0;
      }
      this._frame = "loaded";
      return;
    }
    this._frame = "failed";
  }

  _retry() {
    this._frame = "pending";
    this._iframeUrl = null;
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
    const mode = frameMode(this._iframeUrl, this._frame);
    const seen = kiosk.last_seen
      ? new Date(kiosk.last_seen).toLocaleString()
      : "Not yet";
    const openUrl = this._openUrl || kiosk.url;
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
        <a href=${openUrl} target="_blank" rel="noopener noreferrer">Open in new tab</a>
      </section>
      ${mode === "error"
        ? html`
            <div class="card">
              <h2>Can't load the admin page</h2>
              <p>
                Home Assistant couldn't show this tablet's admin page. Retry the
                connection through Home Assistant, or open the tablet directly.
                Opening it directly may ask the browser to trust the tablet's
                certificate, and a plain HTTP tablet will not load inside an
                HTTPS Home Assistant page.
              </p>
              <div class="actions">
                <button class="button" @click=${this._retry}>Retry</button>
                <a class="button" href=${openUrl} target="_blank" rel="noopener noreferrer"
                  >Open in new tab</a
                >
                <a class="button quiet-link" href=${kiosk.url} target="_blank" rel="noopener noreferrer"
                  >Open tablet directly</a
                >
              </div>
            </div>
          `
        : html`
            <iframe
              title=${`Admin for ${kiosk.name}`}
              src=${this._iframeUrl}
              @error=${() => {
                this._frame = "failed";
              }}
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
    .quiet-link {
      background: transparent;
      color: var(--primary-color, #03a9f4);
      border: 1px solid var(--primary-color, #03a9f4);
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
      background: var(--primary-background-color, #111);
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
      flex-wrap: wrap;
    }
  `;
}

customElements.define("kiosk-mgmt-panel", KioskMgmtPanel);
