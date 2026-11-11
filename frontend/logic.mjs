/**
 * How the sidebar panel decides what to show for one kiosk.
 *
 * The admin page is loaded through Home Assistant's proxy, so the browser
 * talks to Home Assistant and never has to trust the tablet's certificate
 * or mix a plain HTTP tablet into an HTTPS Home Assistant page. A missing
 * proxy URL, or a frame that reports failure, is the in-panel message
 * instead of a blank iframe.
 */

/**
 * @param {string | null | undefined} proxyUrl signed proxy URL
 * @param {"pending" | "loaded" | "failed"} frameState
 * @returns {"iframe" | "error"}
 */
export function frameMode(proxyUrl, frameState) {
  if (!proxyUrl || frameState === "failed") {
    return "error";
  }
  return "iframe";
}

/** How long to wait for the proxied page before showing the retry card. */
export const FRAME_LOAD_TIMEOUT_MS = 8000;

/** postMessage source the proxy hook and error page both use. */
export const PROXY_SOURCE = "kiosk-mgmt-proxy";

/**
 * Pixel height for the panel host.
 *
 * Home Assistant mounts the panel inside `ha-panel-custom`. That element is
 * a block box with safe-area padding and no height of its own, so a
 * `height: 100%` chain collapses and the iframe stays a short strip. The
 * host is sized in pixels instead: down to the bottom of the first ancestor
 * that actually has a box, or the viewport when every ancestor is only as
 * tall as its content.
 *
 * @param {{
 *   top: number,
 *   viewportHeight: number,
 *   bottomInset?: number,
 *   containerBottom?: number | null,
 * }} box
 * @returns {number | null}
 */
export function panelHeightPx(box) {
  const top = Number(box.top);
  const viewportHeight = Number(box.viewportHeight);
  if (!Number.isFinite(top) || !Number.isFinite(viewportHeight)) {
    return null;
  }
  const inset = Number(box.bottomInset);
  const bottomInset = Number.isFinite(inset) ? inset : 0;
  // null means "no stretched ancestor", not a bottom edge at 0.
  const containerBottom =
    box.containerBottom == null ? Number.NaN : Number(box.containerBottom);
  const limit = Number.isFinite(containerBottom)
    ? Math.min(viewportHeight, containerBottom)
    : viewportHeight;
  return Math.max(0, Math.round(limit - top - bottomInset));
}

/**
 * Bottom edge of a container, when the container is tall enough to be the
 * panel column. A content-sized parent (the collapsed case) returns null so
 * the caller uses the viewport instead of locking in the short height.
 *
 * @param {number} height
 * @param {number} viewportHeight
 * @param {number} bottom
 * @returns {number | null}
 */
export function stretchedContainerBottom(height, viewportHeight, bottom) {
  const boxHeight = Number(height);
  const viewport = Number(viewportHeight);
  const edge = Number(bottom);
  if (
    !Number.isFinite(boxHeight) ||
    !Number.isFinite(viewport) ||
    viewport <= 0 ||
    !Number.isFinite(edge)
  ) {
    return null;
  }
  if (boxHeight >= viewport * 0.5) {
    return edge;
  }
  return null;
}

/**
 * Sentence shown above the iframe when a subresource failed.
 *
 * `via` is `"home-assistant"` when the proxy itself refused the request
 * (its error page sends `X-Kiosk-Mgmt-Error: home-assistant`) and `"tablet"`
 * when the status came back from the kiosk.
 *
 * @param {number} status
 * @param {"home-assistant" | "tablet"} via
 * @returns {string}
 */
export function subresourceNotice(status, via) {
  const code = Number(status) || 0;
  if (via === "home-assistant" && (code === 401 || code === 403)) {
    return `Home Assistant refused a request from the admin page (${code}). Refresh Kiosk Mgmt and try again.`;
  }
  if (code === 401 || code === 403) {
    return `The tablet refused a request from the admin page (${code}). Retry the login. Debug logging for Kiosk Mgmt lists the request the tablet refused.`;
  }
  if (code === 502 || code === 503 || code === 504) {
    return `Home Assistant could not reach the tablet for part of this page (${code}). Check that the tablet is on, then retry.`;
  }
  if (code) {
    return `Part of the admin page did not load (${code}). Retry, or turn on debug logging for Kiosk Mgmt and reproduce it.`;
  }
  return "Part of the admin page did not load. Retry, or turn on debug logging for Kiosk Mgmt and reproduce it.";
}
