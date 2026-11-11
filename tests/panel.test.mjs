import assert from "node:assert/strict";
import test from "node:test";

import {
  frameMode,
  panelHeightPx,
  stretchedContainerBottom,
  subresourceNotice,
} from "../frontend/logic.mjs";
import {
  installNetworkShim,
  relocateAuthorization,
  rewriteUrl,
} from "../frontend/shim.mjs";

const CTX = {
  prefix: "/api/kiosk_mgmt/proxy/entry/manual:1",
  origins: ["http://192.0.2.10:2324", "https://192.0.2.10:2324"],
  wsOrigins: ["ws://192.0.2.10:2324", "wss://192.0.2.10:2324"],
  pageProtocol: "https:",
  pageHost: "home.example",
  source: "kiosk-mgmt-proxy",
};

test("a signed proxy URL is shown in the iframe", () => {
  assert.equal(frameMode("/api/kiosk_mgmt/proxy/entry/kiosk/?authSig=1", "pending"), "iframe");
  assert.equal(frameMode("/api/kiosk_mgmt/proxy/entry/kiosk/?authSig=1", "loaded"), "iframe");
});

test("a missing proxy URL is the in-panel message", () => {
  assert.equal(frameMode(null, "pending"), "error");
  assert.equal(frameMode("", "loaded"), "error");
  assert.equal(frameMode(undefined, "pending"), "error");
});

test("a frame that failed to load is the in-panel message", () => {
  assert.equal(
    frameMode("/api/kiosk_mgmt/proxy/entry/kiosk/?authSig=1", "failed"),
    "error",
  );
});

test("the panel fills a collapsed Home Assistant host", () => {
  // ha-panel-custom is only as tall as the short iframe. A tall ancestor,
  // or the viewport, is what the host is sized to.
  assert.equal(stretchedContainerBottom(180, 800, 400), null);
  assert.equal(stretchedContainerBottom(700, 800, 756), 756);
  assert.equal(
    panelHeightPx({
      top: 120,
      viewportHeight: 800,
      bottomInset: 0,
      containerBottom: null,
    }),
    680,
  );
  assert.equal(
    panelHeightPx({
      top: 120,
      viewportHeight: 800,
      bottomInset: 34,
      containerBottom: 756,
    }),
    602,
  );
  assert.equal(
    panelHeightPx({ top: 900, viewportHeight: 800, bottomInset: 0 }),
    0,
  );
  assert.equal(panelHeightPx({ top: Number.NaN, viewportHeight: 800 }), null);
});

test("a failed subresource says who refused it", () => {
  assert.match(subresourceNotice(401, "tablet"), /tablet refused/);
  assert.match(subresourceNotice(401, "home-assistant"), /Home Assistant refused/);
  assert.match(subresourceNotice(502, "tablet"), /could not reach the tablet/);
  assert.match(subresourceNotice(0, "tablet"), /did not load/);
});

test("root-absolute and tablet URLs stay under the proxy prefix", () => {
  const prefix = CTX.prefix;
  assert.equal(rewriteUrl("/api/info", CTX), `${prefix}/api/info`);
  assert.equal(rewriteUrl("/static/app.css", CTX), `${prefix}/static/app.css`);
  assert.equal(
    rewriteUrl("https://192.0.2.10:2324/api/login", CTX),
    `${prefix}/api/login`,
  );
  assert.equal(
    rewriteUrl("wss://192.0.2.10:2324/api/ws?token=abc", CTX),
    `wss://home.example${prefix}/api/ws?token=abc`,
  );
  assert.equal(rewriteUrl(`${prefix}/api/info`, CTX), `${prefix}/api/info`);
  assert.equal(
    rewriteUrl(`wss://home.example${prefix}/api/ws?token=abc`, CTX),
    `wss://home.example${prefix}/api/ws?token=abc`,
  );
  assert.equal(
    rewriteUrl("https://kiosksatellite.com/docs", CTX),
    "https://kiosksatellite.com/docs",
  );
  assert.equal(rewriteUrl("static/app.css", CTX), "static/app.css");
});

test("the tablet token leaves Authorization before Home Assistant sees it", () => {
  const headers = relocateAuthorization({
    Authorization: "Bearer tablet-token",
    "Content-Type": "application/json",
  });
  assert.equal(headers.get("authorization"), null);
  assert.equal(headers.get("X-Kiosk-Authorization"), "Bearer tablet-token");
  assert.equal(headers.get("content-type"), "application/json");
});

test("the shim rewrites fetch, XHR, websocket, EventSource, forms and location", async () => {
  const calls = [];
  const sockets = [];
  const sources = [];
  const assigned = [];
  const submitted = [];
  function OrigWS(url) {
    this.url = String(url);
    sockets.push(this.url);
  }
  OrigWS.CONNECTING = 0;
  OrigWS.OPEN = 1;
  OrigWS.CLOSING = 2;
  OrigWS.CLOSED = 3;
  function OrigES(url) {
    this.url = String(url);
    sources.push(this.url);
  }
  function XHR() {}
  XHR.prototype.open = function open(_method, url) {
    this.opened = String(url);
  };
  XHR.prototype.setRequestHeader = function setRequestHeader(name, value) {
    this.headerName = name;
    this.headerValue = value;
  };
  XHR.prototype.send = function send() {};
  XHR.prototype.addEventListener = function addEventListener() {};

  const messages = [];
  const target = {
    location: {
      protocol: "https:",
      host: "home.example",
      origin: "https://home.example",
      assign(url) {
        assigned.push(String(url));
      },
      replace(url) {
        assigned.push(String(url));
      },
    },
    history: {
      pushState(_state, _title, url) {
        assigned.push(String(url));
      },
      replaceState(_state, _title, url) {
        assigned.push(String(url));
      },
    },
    parent: {
      postMessage(message) {
        messages.push(message);
      },
    },
    document: {
      documentElement: { dataset: {} },
      addEventListener(_type, listener) {
        this.listener = listener;
      },
    },
    navigator: {
      sendBeacon(url) {
        calls.push(String(url));
        return true;
      },
    },
    fetch(input, init) {
      calls.push({ input, authorization: init.headers.get("authorization"), moved: init.headers.get("X-Kiosk-Authorization") });
      return Promise.resolve({ ok: true, status: 200, headers: { get() { return ""; } } });
    },
    XMLHttpRequest: XHR,
    WebSocket: OrigWS,
    EventSource: OrigES,
    open(url) {
      assigned.push(String(url));
    },
  };

  installNetworkShim(target, CTX);
  assert.equal(messages[0].kind, "ready");
  assert.equal(messages[0].ok, true);
  assert.equal(target.document.documentElement.dataset.kioskMgmtShim, "1");

  await target.fetch("/api/info", {
    headers: { Authorization: "Bearer tablet-token" },
  });
  assert.equal(calls[0].input, `${CTX.prefix}/api/info`);
  assert.equal(calls[0].authorization, null);
  assert.equal(calls[0].moved, "Bearer tablet-token");

  const xhr = new target.XMLHttpRequest();
  xhr.open("POST", "/api/update/upload");
  xhr.setRequestHeader("Authorization", "Bearer tablet-token");
  assert.equal(xhr.opened, `${CTX.prefix}/api/update/upload`);
  assert.equal(xhr.headerName, "X-Kiosk-Authorization");

  new target.WebSocket("/api/ws?token=abc");
  assert.equal(sockets[0], `${CTX.prefix}/api/ws?token=abc`);
  new target.WebSocket("wss://192.0.2.10:2324/api/ws?token=abc");
  assert.equal(
    sockets[1],
    `wss://home.example${CTX.prefix}/api/ws?token=abc`,
  );

  new target.EventSource("/api/events");
  assert.equal(sources[0], `${CTX.prefix}/api/events`);

  target.location.assign("/settings");
  target.history.pushState(null, "", "/logs");
  target.open("/static/app.js");
  target.navigator.sendBeacon("/api/health");
  assert.deepEqual(assigned, [
    `${CTX.prefix}/settings`,
    `${CTX.prefix}/logs`,
    `${CTX.prefix}/static/app.js`,
  ]);
  assert.equal(calls.at(-1), `${CTX.prefix}/api/health`);

  const form = {
    action: "",
    getAttribute() {
      return "/api/login";
    },
    setAttribute(_name, value) {
      submitted.push(value);
    },
  };
  target.document.listener({ target: form });
  assert.deepEqual(submitted, [`${CTX.prefix}/api/login`]);
});
