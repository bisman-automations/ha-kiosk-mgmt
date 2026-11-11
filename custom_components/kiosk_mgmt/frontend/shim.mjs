/**
 * Rewrites the Kiosk Satellite admin page so it keeps working under the
 * Home Assistant proxy prefix.
 *
 * The tablet's client (`app/remote-ui/static/core.js` in jxlarrea/kiosk-satellite)
 * already resolves `/api/...` against the directory the page was loaded from,
 * which is this prefix when the iframe is the proxy. It still does two things
 * the prefix alone cannot see:
 *
 * - Every authenticated call, and the APK upload's XMLHttpRequest, sends the
 *   tablet token as `Authorization: Bearer`. Home Assistant reads that header
 *   as its own access token. The shim moves it to `X-Kiosk-Authorization`
 *   before the browser sends the request. The proxy restores it as
 *   `Authorization` on the connection to the tablet, and an invalid bearer
 *   never has to be something Home Assistant accepts.
 * - WebSockets (`new URL(apiUrl("/api/ws?token=…"))` with the page protocol
 *   swapped to ws/wss), EventSource, forms, `location` changes, and any
 *   root-absolute URL such as `/static/...` would otherwise hit Home
 *   Assistant's own origin.
 *
 * Node tests import these functions. The proxy serves the same file as a
 * classic script: it strips the `export` keywords and calls
 * `installNetworkShim(window, ctx)`.
 */

/**
 * @param {string} url
 * @param {{
 *   prefix: string,
 *   origins?: string[],
 *   wsOrigins?: string[],
 *   pageProtocol?: string,
 *   pageHost?: string,
 * }} ctx
 * @returns {string}
 */
export function rewriteUrl(url, ctx) {
  if (typeof url !== "string" || !url || !ctx || !ctx.prefix) {
    return url;
  }
  const prefix = ctx.prefix;
  const origins = ctx.origins || [];
  const wsOrigins = ctx.wsOrigins || [];
  const pageProtocol = ctx.pageProtocol || "";
  const pageHost = ctx.pageHost || "";

  const matches = (value, origin) =>
    value === origin ||
    value.startsWith(`${origin}/`) ||
    value.startsWith(`${origin}?`) ||
    value.startsWith(`${origin}#`);

  for (const origin of origins) {
    if (matches(url, origin)) {
      const rest = url.slice(origin.length);
      return rest ? prefix + rest : `${prefix}/`;
    }
  }
  for (const origin of wsOrigins) {
    if (matches(url, origin)) {
      return wsUrl(prefix, url.slice(origin.length), pageProtocol, pageHost);
    }
  }
  if (pageHost) {
    for (const proto of ["https:", "http:", "wss:", "ws:"]) {
      const origin = `${proto}//${pageHost}`;
      if (!matches(url, origin)) {
        continue;
      }
      const rest = url.slice(origin.length) || "/";
      if (alreadyPrefixed(rest, prefix)) {
        return url;
      }
      if (proto === "ws:" || proto === "wss:") {
        return wsUrl(prefix, rest, pageProtocol, pageHost);
      }
      return prefix + (rest.startsWith("/") ? rest : `/${rest}`);
    }
  }
  if (
    url.startsWith("/") &&
    !url.startsWith("//") &&
    !alreadyPrefixed(url, prefix)
  ) {
    return prefix + url;
  }
  return url;
}

/**
 * @param {string} path
 * @param {string} prefix
 */
function alreadyPrefixed(path, prefix) {
  return (
    path === prefix ||
    path.startsWith(`${prefix}/`) ||
    path.startsWith(`${prefix}?`) ||
    path.startsWith(`${prefix}#`)
  );
}

/**
 * @param {string} prefix
 * @param {string} rest
 * @param {string} pageProtocol
 * @param {string} pageHost
 */
function wsUrl(prefix, rest, pageProtocol, pageHost) {
  const page = pageProtocol === "https:" ? "wss:" : "ws:";
  const path = !rest ? "/" : rest.startsWith("/") ? rest : `/${rest}`;
  return `${page}//${pageHost}${prefix}${path}`;
}

/**
 * Move a tablet `Authorization` header out of the header Home Assistant
 * consumes. Other headers are unchanged.
 *
 * @param {HeadersInit | undefined} headers
 * @returns {Headers}
 */
export function relocateAuthorization(headers) {
  const next = new Headers(headers || undefined);
  const auth = next.get("authorization");
  if (auth) {
    next.delete("authorization");
    next.set("X-Kiosk-Authorization", auth);
  }
  return next;
}

/**
 * Install the hooks on `target` (the iframe's `window` when the proxy
 * serves this file). Safe to call in a unit test with a stand-in window.
 *
 * @param {Window & Record<string, any>} target
 * @param {{
 *   prefix: string,
 *   origins?: string[],
 *   wsOrigins?: string[],
 *   source: string,
 *   pageProtocol?: string,
 *   pageHost?: string,
 * }} ctx
 */
export function installNetworkShim(target, ctx) {
  const rewrite = (url) => rewriteUrl(String(url), ctx);
  const source = ctx.source;
  let reported = false;

  const post = (message) => {
    try {
      const parent = target.parent || target;
      parent.postMessage(message, target.location.origin);
    } catch (_err) {
      // The panel is same-origin. A test stand-in with no parent still installed.
    }
  };

  const report = (url, status, viaHeader) => {
    const path = String(url || "").split("?")[0];
    // A wrong password is a 401 from /api/login and the page shows that itself.
    if (!path || path.endsWith("/api/login") || reported) {
      return;
    }
    reported = true;
    post({
      source,
      ok: false,
      kind: "subresource",
      status: Number(status) || 0,
      via: viaHeader === "home-assistant" ? "home-assistant" : "tablet",
    });
  };

  const origFetch = typeof target.fetch === "function" ? target.fetch.bind(target) : null;
  if (origFetch) {
    target.fetch = (input, init) => {
      let next = input;
      let nextInit = init;
      if (typeof input === "string" || (typeof URL !== "undefined" && input instanceof URL)) {
        next = rewrite(input);
        if (init) {
          nextInit = { ...init, headers: relocateAuthorization(init.headers) };
        }
      } else if (input && typeof input.url === "string") {
        const headers = relocateAuthorization(input.headers);
        if (init && init.headers) {
          relocateAuthorization(init.headers).forEach((value, key) => {
            headers.set(key, value);
          });
        }
        try {
          next = new Request(rewrite(input.url), input);
        } catch (_err) {
          next = rewrite(input.url);
        }
        nextInit = { ...(init || {}), headers };
      }
      return Promise.resolve(origFetch(next, nextInit)).then((response) => {
        if (response && response.status >= 400) {
          const via =
            response.headers && typeof response.headers.get === "function"
              ? response.headers.get("X-Kiosk-Mgmt-Error")
              : "";
          const failedUrl = typeof next === "string" ? next : next && next.url;
          report(failedUrl, response.status, via);
        }
        return response;
      });
    };
  }

  const xhrProto = target.XMLHttpRequest && target.XMLHttpRequest.prototype;
  if (xhrProto && typeof xhrProto.open === "function") {
    const origOpen = xhrProto.open;
    xhrProto.open = function open(method, url, ...rest) {
      return origOpen.call(this, method, rewrite(url), ...rest);
    };
  }
  if (xhrProto && typeof xhrProto.setRequestHeader === "function") {
    const origSet = xhrProto.setRequestHeader;
    xhrProto.setRequestHeader = function setRequestHeader(name, value) {
      if (String(name).toLowerCase() === "authorization") {
        return origSet.call(this, "X-Kiosk-Authorization", value);
      }
      return origSet.call(this, name, value);
    };
  }
  if (xhrProto && typeof xhrProto.send === "function") {
    const origSend = xhrProto.send;
    xhrProto.send = function send(...args) {
      if (typeof this.addEventListener === "function") {
        this.addEventListener("load", () => {
          if (this.status >= 400) {
            const via =
              typeof this.getResponseHeader === "function"
                ? this.getResponseHeader("X-Kiosk-Mgmt-Error")
                : "";
            report(this.responseURL, this.status, via);
          }
        });
      }
      return origSend.apply(this, args);
    };
  }

  if (typeof target.WebSocket === "function") {
    const OrigWS = target.WebSocket;
    function ProxiedWebSocket(url, protocols) {
      const next = rewrite(url);
      if (protocols === undefined) {
        return new OrigWS(next);
      }
      return new OrigWS(next, protocols);
    }
    ProxiedWebSocket.prototype = OrigWS.prototype;
    ProxiedWebSocket.CONNECTING = OrigWS.CONNECTING;
    ProxiedWebSocket.OPEN = OrigWS.OPEN;
    ProxiedWebSocket.CLOSING = OrigWS.CLOSING;
    ProxiedWebSocket.CLOSED = OrigWS.CLOSED;
    target.WebSocket = ProxiedWebSocket;
  }

  if (typeof target.EventSource === "function") {
    const OrigES = target.EventSource;
    function ProxiedEventSource(url, options) {
      return new OrigES(rewrite(url), options);
    }
    ProxiedEventSource.prototype = OrigES.prototype;
    target.EventSource = ProxiedEventSource;
  }

  if (target.history) {
    if (typeof target.history.pushState === "function") {
      const origPush = target.history.pushState.bind(target.history);
      target.history.pushState = (state, title, url) =>
        origPush(state, title, typeof url === "string" ? rewrite(url) : url);
    }
    if (typeof target.history.replaceState === "function") {
      const origReplace = target.history.replaceState.bind(target.history);
      target.history.replaceState = (state, title, url) =>
        origReplace(state, title, typeof url === "string" ? rewrite(url) : url);
    }
  }

  if (target.location) {
    if (typeof target.location.assign === "function") {
      const origAssign = target.location.assign.bind(target.location);
      target.location.assign = (url) => origAssign(rewrite(url));
    }
    if (typeof target.location.replace === "function") {
      const origReplaceLoc = target.location.replace.bind(target.location);
      target.location.replace = (url) => origReplaceLoc(rewrite(url));
    }
  }

  if (typeof target.open === "function") {
    const origOpenWindow = target.open.bind(target);
    target.open = (url, name, features) =>
      origOpenWindow(url ? rewrite(url) : url, name, features);
  }

  const beacon = target.navigator && target.navigator.sendBeacon;
  if (typeof beacon === "function") {
    const origBeacon = beacon.bind(target.navigator);
    target.navigator.sendBeacon = (url, data) => origBeacon(rewrite(url), data);
  }

  const doc = target.document;
  if (doc && typeof doc.addEventListener === "function") {
    doc.addEventListener(
      "submit",
      (event) => {
        const form = event.target;
        if (!form || typeof form.getAttribute !== "function") {
          return;
        }
        const action = form.getAttribute("action");
        if (action) {
          form.setAttribute("action", rewrite(action));
        }
      },
      true,
    );
  }

  if (doc && doc.documentElement && doc.documentElement.dataset) {
    doc.documentElement.dataset.kioskMgmtShim = "1";
  }

  post({ source, ok: true, kind: "ready" });
}
