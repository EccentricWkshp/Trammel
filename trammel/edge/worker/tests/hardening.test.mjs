// Hardening the edge portal (0.9.2): a bot check on signing in, limits on
// sending and on what waits for Trammel, cleanup, and telling Trammel when its
// database is a new one.
import assert from "node:assert/strict";
import { test } from "node:test";

import { _resetSchemaFlag } from "../db.js";
import worker from "../worker.js";
import { browser, cookieFrom, env, fakeMailServer, syncRequest } from "./fakes.mjs";
import { accessHash } from "../crypto.js";

const CUSTOMER = "01900000-0000-7000-8000-000000000001";

async function shop(extra = {}, site = {}) {
  const environment = env(extra);
  const records = [
    {
      type: "config",
      id: "site",
      data: {
        portal_on: true,
        css: "portal-abc.css",
        business: { name: "Test Shop" },
        email: { enabled: true, subject: "Code", body: "Code %%CODE%%" },
        uploads: { enabled: true, limit_mb: 1, per_send: 10, kinds_label: "files" },
        hooks: { shopify: true },
        ...site,
      },
    },
    { type: "customer", id: CUSTOMER, data: { name: "Foundry", signers: [{ name: "Dana", email: "dana@example.com" }] } },
    {
      type: "access",
      id: await accessHash(environment, "ABCD1234EFGH"),
      customer: CUSTOMER,
      data: { expires_at: new Date(Date.now() + 86400000).toISOString(), given_by: "Ada", code_id: "c1" },
    },
  ];
  await worker.fetch(await syncRequest(environment, "POST", "/_sync/records", { upsert: records, delete: [] }), environment);
  return environment;
}

test("the database says which one it is; a new one says so", async () => {
  const environment = env();
  const first = await (await worker.fetch(await syncRequest(environment, "GET", "/_sync/health"), environment)).json();
  const again = await (await worker.fetch(await syncRequest(environment, "GET", "/_sync/health"), environment)).json();
  assert.equal(first.instance, again.instance);
  const fresh = env();
  const other = await (await worker.fetch(await syncRequest(fresh, "GET", "/_sync/health"), fresh)).json();
  assert.notEqual(other.instance, first.instance);
});

test("with a bot check, signing in needs it passed", async () => {
  const environment = await shop({ TURNSTILE_SECRET: "ts-secret" }, { turnstile: "0x4AAA-site" });
  const mail = fakeMailServer();
  globalThis.__connect = mail.connect;
  const page = await worker.fetch(browser("GET", "/portal/sign-in/"), environment);
  assert.match(page.headers.get("Content-Security-Policy"), /script-src 'self' https:\/\/challenges.cloudflare.com/);
  const text = await page.text();
  assert.match(text, /class="cf-turnstile" data-sitekey="0x4AAA-site"/);

  let response = await worker.fetch(browser("POST", "/portal/sign-in/", { form: { email: "dana@example.com" } }), environment);
  assert.match(response.headers.get("Location"), /notice=bot-check/);
  assert.equal(mail.messages.length, 0);

  const checked = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    checked.push([url, init.body.get("secret"), init.body.get("response")]);
    return new Response(JSON.stringify({ success: init.body.get("response") === "good" }));
  };
  try {
    response = await worker.fetch(browser("POST", "/portal/sign-in/", { form: { email: "dana@example.com", "cf-turnstile-response": "bad" } }), environment);
    assert.match(response.headers.get("Location"), /notice=bot-check/);
    response = await worker.fetch(browser("POST", "/portal/sign-in/", { form: { email: "dana@example.com", "cf-turnstile-response": "good" } }), environment);
    assert.match(response.headers.get("Location"), /^\/portal\/sign-in\/code\/\?email=/);
    assert.equal(mail.messages.length, 1);
    response = await worker.fetch(browser("POST", "/portal/sign-in/access-code/", { form: { code: "ABCD1234EFGH", "cf-turnstile-response": "good" } }), environment);
    assert.equal(response.headers.get("Location"), "/portal/");
    // Pages past signing in don't allow the bot check's scripts.
    const home = await worker.fetch(browser("GET", "/portal/", { cookie: cookieFrom(response) }), environment);
    assert.doesNotMatch(home.headers.get("Content-Security-Policy"), /challenges/);
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(checked[0][0], "https://challenges.cloudflare.com/turnstile/v0/siteverify");
  assert.equal(checked[0][1], "ts-secret");
});

test("sending is limited per address, and what waits for Trammel is capped", async () => {
  const environment = await shop();
  const signedIn = await worker.fetch(browser("POST", "/portal/sign-in/access-code/", { form: { code: "ABCD1234EFGH" } }), environment);
  const cookie = cookieFrom(signedIn);
  const send = () => {
    const body = new FormData();
    body.append("files", new File(["x"], "a.png"));
    return worker.fetch(new Request("https://portal.example.com/portal/send/", {
      method: "POST",
      headers: { Cookie: cookie, Origin: "https://portal.example.com", "CF-Connecting-IP": "198.51.100.7" },
      body,
    }), environment);
  };
  for (let i = 0; i < 20; i++) assert.match((await send()).headers.get("Location"), /files-sent/);
  assert.match((await send()).headers.get("Location"), /send-too-often/);

  // Store notices have their own room: customers' activity doesn't fill it,
  // and a full one doesn't stop customers (SEC-08, 0.10.6).
  const db = environment.DB.raw;
  const signed = () =>
    worker.fetch(new Request("https://portal.example.com/imports/shopify/webhook/", {
      method: "POST", headers: { "X-Shopify-Hmac-Sha256": "sig==" }, body: "{}",
    }), environment);
  const customerActivity = db.prepare("INSERT INTO inbox (kind, at, data) VALUES ('x', '2026-01-01', '{}')");
  for (let i = 0; i < 5000; i++) customerActivity.run();
  assert.equal((await signed()).status, 200);
  const waiting = db.prepare("INSERT INTO inbox (kind, at, data) VALUES ('shopify_webhook', '2026-01-01', '{}')");
  for (let i = 0; i < 1000; i++) waiting.run();
  assert.equal((await signed()).status, 503);
});

test("store notices need a signature, Shopify's checked here, and are rate-limited (SEC-08)", async () => {
  const { shopifySignature } = await import("../crypto.js");
  const environment = await shop({ SHOPIFY_WEBHOOK_SECRET: "shpss_test" }, { hooks: { shopify: true, ebay_deletion: true } });
  environment.EBAY_TOKEN = "verify-token-123";
  const post = (path, headers, body = '{"id": 1}', ip = "203.0.113.5") =>
    worker.fetch(new Request(`https://portal.example.com${path}`, {
      method: "POST", headers: { "CF-Connecting-IP": ip, ...headers }, body,
    }), environment);
  const shopify = "/imports/shopify/webhook/";
  assert.equal((await post(shopify, {})).status, 401);
  assert.equal((await post(shopify, { "X-Shopify-Hmac-Sha256": "forged==" })).status, 401);
  const good = await shopifySignature("shpss_test", '{"id": 1}');
  assert.equal((await post(shopify, { "X-Shopify-Hmac-Sha256": good })).status, 200);
  assert.equal((await post("/imports/ebay/deletion/", {})).status, 401);
  const rows = environment.DB.raw.prepare("SELECT kind FROM inbox WHERE kind IN ('shopify_webhook', 'ebay_deletion')").all();
  assert.equal(rows.length, 1);

  const attempts = environment.DB.raw.prepare(
    "INSERT INTO attempts (kind, email, ip_key, at, ok) VALUES ('hook', '', ?, ?, 1)",
  );
  for (let i = 0; i < 600; i++) attempts.run("198.51.100.9", new Date().toISOString());
  assert.equal((await post(shopify, { "X-Shopify-Hmac-Sha256": good }, '{"id": 1}', "198.51.100.9")).status, 429);
});

test("files never collected are cleared after a month", async () => {
  const environment = await shop();
  await environment.FILES.put("upload/old/a.png", "a");
  await environment.FILES.put("upload/new/b.png", "b");
  await environment.FILES.put("assets/portal-abc.css", "c");
  environment.FILES.store.get("upload/old/a.png").uploaded = new Date(Date.now() - 31 * 24 * 3600 * 1000);
  // Their "on its way" entries go too; what Trammel published stays.
  const old = new Date(Date.now() - 31 * 24 * 3600 * 1000).toISOString();
  const db = environment.DB.raw;
  const add = db.prepare("INSERT INTO records (type, id, customer, job, sort, data) VALUES ('upload', ?, NULL, NULL, ?, ?)");
  add.run("old", old, JSON.stringify({ name: "a.png", status: "Sending to us…" }));
  add.run("new", new Date().toISOString(), JSON.stringify({ name: "b.png", status: "Sending to us…" }));
  add.run("taken", old, JSON.stringify({ name: "c.png", status: "Accepted" }));
  _resetSchemaFlag();
  await worker.scheduled({}, environment);
  assert.deepEqual([...environment.FILES.store.keys()].sort(), ["assets/portal-abc.css", "upload/new/b.png"]);
  const left = db.prepare("SELECT id FROM records WHERE type = 'upload' ORDER BY id").all().map((r) => r.id);
  assert.deepEqual(left, ["new", "taken"]);
});

test("guesses at the same moment get no more than a code's five tries (SEC-06)", async () => {
  const { codeHash } = await import("../crypto.js");
  const { verifyCode } = await import("../auth.js");
  const environment = await shop();
  const email = "dana@example.com";
  environment.DB.raw
    .prepare("INSERT INTO codes (id, email, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)")
    .run("code1", email, await codeHash(environment, email, "987654"), new Date().toISOString(), new Date(Date.now() + 300000).toISOString());
  const results = await Promise.all(
    Array.from({ length: 6 }, (_, n) =>
      verifyCode(environment, email, n === 5 ? "987654" : String(n).padStart(6, "0"), `198.51.100.${n + 1}`),
    ),
  );
  const code = environment.DB.raw.prepare("SELECT tries FROM codes WHERE id = ?").get("code1");
  assert.equal(code.tries, 5);
  // The sixth came after five tries were spent: refused, though it was right.
  assert.equal(results[5].length, 0);
});
