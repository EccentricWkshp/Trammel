// The edge portal Worker, end to end under Node: Trammel's signed sync calls,
// signing in by emailed code and by a code from staff, and what each page shows.
import assert from "node:assert/strict";
import { test } from "node:test";

import { accessHash, syncSignature } from "../crypto.js";
import { message, sendMail } from "../smtp.js";
import worker from "../worker.js";
import { browser, cookieFrom, decodeMessage, env, fakeMailServer, syncRequest } from "./fakes.mjs";

const CUSTOMER = "01900000-0000-7000-8000-000000000001";
const OTHER = "01900000-0000-7000-8000-000000000002";
const JOB = "01900000-0000-7000-8000-000000000010";
const QUOTE = "01900000-0000-7000-8000-000000000020";
const OTHER_QUOTE = "01900000-0000-7000-8000-000000000021";

const SITE = {
  type: "config",
  id: "site",
  data: {
    portal_on: true,
    css: "portal-abc.css",
    business: { name: "Test Shop", phone: "(253) 555-0100", email: "shop@example.com", logo: "" },
    email: {
      enabled: true,
      subject: "Your sign-in code for Test Shop",
      body: "Your code is %%CODE%%. It expires in %%MINUTES%% minutes.\n",
    },
  },
};

async function publish(environment, upsert, remove = []) {
  const response = await worker.fetch(
    await syncRequest(environment, "POST", "/_sync/records", { upsert, delete: remove }),
    environment,
  );
  assert.equal(response.status, 200);
}

async function shop() {
  const environment = env();
  await publish(environment, [
    SITE,
    {
      type: "customer",
      id: CUSTOMER,
      data: { name: "Foundry Brewing", signers: [{ name: "Dana Price", email: "dana@example.com" }] },
    },
    { type: "customer", id: OTHER, data: { name: "Lantern Café", signers: [{ name: "Marco", email: "marco@example.com" }] } },
    {
      type: "job",
      id: JOB,
      customer: CUSTOMER,
      sort: "2026-11-01",
      data: { number: "J-2026-0016", title: "Taproom boards", status: "In progress", promised: "11/24/2026" },
    },
    {
      type: "quote",
      id: QUOTE,
      customer: CUSTOMER,
      job: JOB,
      sort: "2026-11-02",
      data: {
        kind: "Quote", noun: "quote", number: "Q-2026-0002", title: "Taproom boards", total: "$1,250.00",
        status: "Sent", open: true, valid_until: "12/01/2026", doc: `doc/quote/${QUOTE}/1.html`,
        pdf: `doc/quote/${QUOTE}/1.pdf`, pdf_name: "Q-2026-0002-r1.pdf", job_number: "J-2026-0016",
      },
    },
    {
      type: "quote",
      id: OTHER_QUOTE,
      customer: OTHER,
      sort: "2026-11-03",
      data: { kind: "Quote", noun: "quote", number: "Q-2026-0009", title: "Clips", total: "$80.00", status: "Sent", open: true },
    },
  ]);
  const put = async (key, body, type) => {
    const response = await worker.fetch(
      await syncRequest(environment, "PUT", `/_sync/objects/${key}`, new TextEncoder().encode(body), {
        "Content-Type": type,
      }),
      environment,
    );
    assert.equal(response.status, 200);
  };
  await put(`doc/quote/${QUOTE}/1.html`, "<html><body><h1>Quote Q-2026-0002</h1></body></html>", "text/html");
  await put(`doc/quote/${QUOTE}/1.pdf`, "%PDF-1.7 fake", "application/pdf");
  await put("assets/portal-abc.css", "body{}", "text/css");
  return environment;
}

test("Trammel's signature matches the vector both sides test against", async () => {
  const body = new TextEncoder().encode('{"a":1}');
  assert.equal(
    await syncSignature("test-key", "1700000000", "POST", "/_sync/records", body),
    "ae5b06011cc6a51d2ebe237eb989c8e13ec2f1507f106b4d91648b5b9418a4dd",
  );
});

test("sync calls must be signed, and fresh", async () => {
  const environment = env();
  const unsigned = await worker.fetch(new Request("https://portal.example.com/_sync/health"), environment);
  assert.equal(unsigned.status, 403);
  const wrongKey = await syncRequest({ ...environment, SYNC_KEY: "another" }, "GET", "/_sync/health");
  assert.equal((await worker.fetch(wrongKey, environment)).status, 403);
  const health = await worker.fetch(await syncRequest(environment, "GET", "/_sync/health"), environment);
  assert.equal(health.status, 200);
  const body = await health.json();
  assert.match(body.instance, /^[0-9a-f-]{36}$/);
  assert.deepEqual({ ...body, instance: "" }, { ok: true, version: "0.9.0", schema: 1, instance: "", records: 0, inbox: 0 });
});

test("the portal is closed until Trammel opens it", async () => {
  const environment = env();
  const response = await worker.fetch(browser("GET", "/portal/"), environment);
  assert.equal(response.status, 503);
  assert.match(await response.text(), /portal is closed/);
});

test("an emailed code signs the customer in, and the inbox records it", async () => {
  const environment = await shop();
  const mail = fakeMailServer();
  globalThis.__connect = mail.connect;
  let response = await worker.fetch(browser("POST", "/portal/sign-in/", { form: { email: "Dana@Example.com" } }), environment);
  assert.equal(response.status, 303);
  assert.match(response.headers.get("Location"), /^\/portal\/sign-in\/code\/\?email=dana%40example.com/);
  assert.equal(mail.messages.length, 1);
  assert.deepEqual(mail.tls, ["on"]);
  const text = decodeMessage(mail.messages[0]);
  const code = text.match(/(\d{3}) (\d{3})/).slice(1).join("");
  assert.match(text, /expires in 10 minutes/);

  // A wrong code doesn't sign in; the right one does, once.
  response = await worker.fetch(browser("POST", "/portal/sign-in/code/", { form: { email: "dana@example.com", code: "000000" } }), environment);
  assert.match(response.headers.get("Location"), /notice=code-wrong/);
  response = await worker.fetch(browser("POST", "/portal/sign-in/code/", { form: { email: "dana@example.com", code } }), environment);
  assert.equal(response.headers.get("Location"), "/portal/");
  const cookie = cookieFrom(response);
  assert.match(response.headers.get("Set-Cookie"), /HttpOnly; SameSite=Lax/);
  const again = await worker.fetch(browser("POST", "/portal/sign-in/code/", { form: { email: "dana@example.com", code } }), environment);
  assert.match(again.headers.get("Location"), /code-wrong/);

  // Their account: their quote waiting, their job; not another customer's.
  const home = await (await worker.fetch(browser("GET", "/portal/", { cookie }), environment)).text();
  assert.match(home, /Hello, Dana Price/);
  assert.match(home, /Q-2026-0002/);
  assert.match(home, /J-2026-0016 · Taproom boards/);
  assert.doesNotMatch(home, /Q-2026-0009/);
  const theirs = await worker.fetch(browser("GET", `/portal/quotes/${OTHER_QUOTE}/`, { cookie }), environment);
  assert.equal(theirs.status, 404);
  const quote = await worker.fetch(browser("GET", `/portal/quotes/${QUOTE}/`, { cookie }), environment);
  assert.match(await quote.text(), /iframe class="document-preview" src="\/portal\/quotes\/[^"]+\/document\/"/);
  const doc = await worker.fetch(browser("GET", `/portal/quotes/${QUOTE}/document/`, { cookie }), environment);
  assert.match(doc.headers.get("Content-Security-Policy"), /sandbox/);
  assert.match(await doc.text(), /Quote Q-2026-0002/);
  const pdf = await worker.fetch(browser("GET", `/portal/quotes/${QUOTE}/pdf/`, { cookie }), environment);
  assert.equal(pdf.headers.get("Content-Disposition"), 'attachment; filename="Q-2026-0002-r1.pdf"');

  const pulled = await worker.fetch(await syncRequest(environment, "GET", "/_sync/inbox?after=0"), environment);
  const { events } = await pulled.json();
  assert.deepEqual(events.map((e) => e.kind), ["code_sent", "signed_in"]);
  assert.equal(events[1].data.customer, CUSTOMER);
  assert.equal(events[1].ip, "203.0.113.9");
  await worker.fetch(await syncRequest(environment, "POST", "/_sync/ack", { upto: events[1].seq }), environment);
  const after = await (await worker.fetch(await syncRequest(environment, "GET", "/_sync/inbox?after=0"), environment)).json();
  assert.equal(after.events.length, 0);

  // Access turned off in Trammel: the customer is unpublished, the session ends.
  await publish(environment, [], [{ type: "customer", id: CUSTOMER }]);
  const gone = await worker.fetch(browser("GET", "/portal/", { cookie }), environment);
  assert.equal(gone.headers.get("Location"), "/portal/sign-in/");
});

test("addresses can't be probed, and asking is rate-limited", async () => {
  const environment = await shop();
  const mail = fakeMailServer();
  globalThis.__connect = mail.connect;
  const unknown = await worker.fetch(browser("POST", "/portal/sign-in/", { form: { email: "nobody@example.com" } }), environment);
  assert.match(unknown.headers.get("Location"), /notice=code-sent/);
  assert.equal(mail.messages.length, 0);
  for (let i = 0; i < 5; i++) {
    await worker.fetch(browser("POST", "/portal/sign-in/", { form: { email: "dana@example.com" } }), environment);
  }
  assert.equal(mail.messages.length, 3); // three in 15 minutes
});

test("STARTTLS, and a failed sign-in to the mail server is reported, not hidden", async () => {
  const mail = fakeMailServer();
  await sendMail(
    { host: "m", port: 587, security: "starttls", username: "u", password: "p", from_address: "shop@example.com" },
    { to: "dana@example.com", subject: "Héllo", text: "Line one\n.dot line" },
    { connectTo: mail.connect },
  );
  assert.deepEqual(mail.tls, ["starttls", "upgraded"]);
  assert.match(mail.messages[0], /Subject: =\?UTF-8\?B\?/);
  assert.equal(decodeMessage(mail.messages[0]), "Line one\r\n.dot line");
  await assert.rejects(
    sendMail({ host: "m", port: 25, security: "none", from_address: "a@b.c" }, { to: "x@y.z", subject: "s", text: "t" }),
    /must be encrypted/,
  );
  const failing = fakeMailServer({ failAuth: true });
  await assert.rejects(
    sendMail(
      { host: "m", port: 465, security: "ssl", username: "u", password: "p", from_address: "a@b.c" },
      { to: "x@y.z", subject: "s", text: "t" },
      { connectTo: failing.connect },
    ),
    /535/,
  );
  assert.match(message({ fromAddress: "a@b.c", fromName: "", to: "x", subject: "s", text: "t", id: "1" }), /Message-ID: <1@b.c>/);
});

test("a code from staff signs in once", async () => {
  const environment = await shop();
  const code = "ABCD1234EFGH";
  await publish(environment, [
    {
      type: "access",
      id: await accessHash(environment, code),
      customer: CUSTOMER,
      data: { expires_at: new Date(Date.now() + 86400000).toISOString(), given_by: "Ada Admin", code_id: "c1" },
    },
  ]);
  let response = await worker.fetch(browser("POST", "/portal/sign-in/access-code/", { form: { code: "abcd-1234-efgh" } }), environment);
  assert.equal(response.headers.get("Location"), "/portal/");
  const cookie = cookieFrom(response);
  const home = await (await worker.fetch(browser("GET", "/portal/", { cookie }), environment)).text();
  assert.match(home, /Hello, Foundry Brewing/);
  response = await worker.fetch(browser("POST", "/portal/sign-in/access-code/", { form: { code } }), environment);
  assert.match(response.headers.get("Location"), /access-wrong/);
  const { events } = await (await worker.fetch(await syncRequest(environment, "GET", "/_sync/inbox"), environment)).json();
  assert.deepEqual(events.map((e) => [e.kind, e.data.code]), [["access_code_used", "c1"]]);
});

test("someone at several customers chooses one", async () => {
  const environment = await shop();
  await publish(environment, [
    { type: "customer", id: OTHER, data: { name: "Lantern Café", signers: [{ name: "Dana P.", email: "dana@example.com" }] } },
  ]);
  const mail = fakeMailServer();
  globalThis.__connect = mail.connect;
  await worker.fetch(browser("POST", "/portal/sign-in/", { form: { email: "dana@example.com" } }), environment);
  const code = decodeMessage(mail.messages[0]).match(/(\d{3}) (\d{3})/).slice(1).join("");
  let response = await worker.fetch(browser("POST", "/portal/sign-in/code/", { form: { email: "dana@example.com", code } }), environment);
  assert.equal(response.headers.get("Location"), "/portal/sign-in/choose/");
  const cookie = cookieFrom(response);
  const choosing = await (await worker.fetch(browser("GET", "/portal/", { cookie }), environment)).status;
  assert.equal(choosing, 303);
  response = await worker.fetch(browser("POST", "/portal/sign-in/choose/", { form: { customer: OTHER }, cookie }), environment);
  assert.equal(response.headers.get("Location"), "/portal/");
  const home = await (await worker.fetch(browser("GET", "/portal/", { cookie }), environment)).text();
  assert.match(home, /Hello, Dana P\./);
  assert.match(home, /Q-2026-0009/);
});

test("posts from elsewhere are refused, and purge empties everything", async () => {
  const environment = await shop();
  const forged = new Request("https://portal.example.com/portal/sign-in/", {
    method: "POST",
    headers: { Origin: "https://evil.example", "Content-Type": "application/x-www-form-urlencoded" },
    body: "email=dana%40example.com",
  });
  assert.equal((await worker.fetch(forged, environment)).status, 403);
  const purged = await worker.fetch(await syncRequest(environment, "POST", "/_sync/purge", {}), environment);
  assert.equal((await purged.json()).objects, 3);
  assert.equal(environment.FILES.store.size, 0);
  const closed = await worker.fetch(browser("GET", "/portal/"), environment);
  assert.equal(closed.status, 503);
});
