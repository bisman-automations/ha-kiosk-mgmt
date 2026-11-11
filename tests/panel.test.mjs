import assert from "node:assert/strict";
import test from "node:test";

import { frameMode } from "../frontend/logic.mjs";

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
