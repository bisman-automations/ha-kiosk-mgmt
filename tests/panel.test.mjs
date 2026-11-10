import assert from "node:assert/strict";
import test from "node:test";

import { frameMode } from "../frontend/logic.mjs";

test("an HTTPS Home Assistant page does not embed a plain HTTP kiosk", () => {
  assert.equal(frameMode("https:", { scheme: "http" }, "pending"), "mixed");
  assert.equal(frameMode("https:", { scheme: "http" }, "loaded"), "mixed");
});

test("HTTP Home Assistant embeds an HTTP kiosk", () => {
  assert.equal(frameMode("http:", { scheme: "http" }, "pending"), "iframe");
});

test("HTTPS kiosk stays in the frame until the load times out", () => {
  assert.equal(frameMode("https:", { scheme: "https" }, "pending"), "iframe");
  assert.equal(frameMode("https:", { scheme: "https" }, "loaded"), "iframe");
});

test("an HTTPS frame that never loads offers the trust card", () => {
  assert.equal(frameMode("https:", { scheme: "https" }, "failed"), "trust");
  assert.equal(frameMode("http:", { scheme: "https" }, "failed"), "trust");
});
