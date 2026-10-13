import test from "node:test";
import assert from "node:assert/strict";
import { requestApi, ApiError } from "../src/lib/api.js";

test("reverse proxy HTML errors produce a readable message", async () => {
  await assert.rejects(
    requestApi("/status", "GET", undefined, {
      fetch: async () =>
        new Response("<html>Bad Gateway</html>", { status: 502 }),
    }),
    (error) =>
      error instanceof ApiError &&
      error.status === 502 &&
      error.message.includes("服务器暂时无法连接") &&
      !error.message.includes("<html>"),
  );
});

test("timed-out writes are marked uncertain instead of being retried", async () => {
  let attempts = 0;
  const fetch = (_url, options) =>
    new Promise((_resolve, reject) => {
      attempts++;
      options.signal.addEventListener("abort", () =>
        reject(new DOMException("Aborted", "AbortError")),
      );
    });
  await assert.rejects(
    requestApi("/tasks/claim", "POST", {}, { fetch, timeout: 5 }),
    (error) => error.uncertain && error.message.includes("避免重复提交"),
  );
  assert.equal(attempts, 1);
});

test("multipart uploads allow the browser to generate the boundary and preserve the operation key", async () => {
  const body = new FormData();
  body.append("file", new Blob(["test"]), "farm.saz");
  let options;
  await requestApi("/authorization/saz", "POST", body, {
    key: "stable-operation-key",
    fetch: async (_url, value) => {
      options = value;
      return new Response('{"ok":true}');
    },
  });
  assert.equal(options.body, body);
  assert.equal(options.headers["Content-Type"], undefined);
  assert.equal(options.headers["Idempotency-Key"], "stable-operation-key");
});
