/**
 * How the sidebar panel decides what to show for one kiosk.
 *
 * An HTTPS Home Assistant page cannot embed a plain HTTP admin page
 * (mixed content). An HTTPS admin page whose frame never loads is treated
 * as an untrusted certificate: the browser does not report that failure
 * to the parent page, so a timeout stands in for it.
 */

/**
 * @param {string} pageProtocol `location.protocol`, such as "https:".
 * @param {{ scheme?: string }} kiosk
 * @param {"pending" | "loaded" | "failed"} frameState
 * @returns {"mixed" | "trust" | "iframe"}
 */
export function frameMode(pageProtocol, kiosk, frameState) {
  if (pageProtocol === "https:" && kiosk.scheme === "http") {
    return "mixed";
  }
  if (kiosk.scheme === "https" && frameState === "failed") {
    return "trust";
  }
  return "iframe";
}

/** How long to wait for an HTTPS frame before offering the trust card. */
export const FRAME_LOAD_TIMEOUT_MS = 8000;
