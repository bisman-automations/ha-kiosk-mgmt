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
