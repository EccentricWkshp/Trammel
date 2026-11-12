// What customers do on the edge portal (0.9.1): answer a quote with their
// choices, the terms and a signature; send files; and the online stores'
// webhooks, callbacks and deletion notices through the Worker.
import assert from "node:assert/strict";
import { test } from "node:test";

import { accessHash, sha256Hex } from "../crypto.js";
import worker from "../worker.js";
import { browser, cookieFrom, env, syncRequest } from "./fakes.mjs";

const CUSTOMER = "01900000-0000-7000-8000-000000000001";
const JOB = "01900000-0000-7000-8000-000000000010";
const QUOTE = "01900000-0000-7000-8000-000000000020";
const WALNUT = "01900000-0000-7000-8000-0000000000a1";
const MAPLE = "01900000-0000-7000-8000-0000000000a2";
const BENCH = "01900000-0000-7000-8000-0000000000b1";
const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

async function call(environment, request) {
  return worker.fetch(request, environment);
}

async function shop({ uploads = true, hooks = {} } = {}) {
  const environment = env({ EBAY_TOKEN: "verify-token-123" });
  const records = [
    {
      type: "config",
      id: "site",
      data: {
        portal_on: true,
        css: "portal-abc.css",
        signature_js: "signature-abc.js",
        local_url: "https://trammel.home.arpa:8727",
        business: { name: "Test Shop" },
        email: { enabled: false },
        uploads: uploads
          ? { enabled: true, limit_mb: 1, per_send: 2, kinds_label: "photos and DXF", accept: ".png,.dxf" }
          : { enabled: false },
        hooks,
      },
    },
    { type: "customer", id: CUSTOMER, data: { name: "Foundry Brewing", signers: [] } },
    { type: "job", id: JOB, customer: CUSTOMER, sort: "1", data: { number: "J-1", title: "Table", status: "Quote sent" } },
    {
      type: "quote",
      id: QUOTE,
      customer: CUSTOMER,
      job: JOB,
      sort: "1",
      data: {
        kind: "Quote", noun: "quote", number: "Q-1", title: "Table", total: "$1,800.00", status: "Sent", open: true,
        respond: {
          signing: true,
          agreement: { title: "Customer agreement", text: "Pay on time.", hash: "abc123" },
          choices: {
            groups: [{ name: "Wood", default: WALNUT, lines: [
              { id: WALNUT, description: "Walnut top", price: "$800.00" },
              { id: MAPLE, description: "Maple top", price: "$500.00" },
            ] }],
            options: [{ id: BENCH, description: "Two benches", price: "$900.00" }],
          },
          totals: {
            [[WALNUT].sort().join(",")]: { total: "$1,800.00", deposit: "$900.00" },
            [[MAPLE, BENCH].sort().join(",")]: { total: "$2,400.00", deposit: "$1,200.00" },
          },
        },
      },
    },
    {
      type: "access",
      id: await accessHash(environment, "ABCD1234EFGH"),
      customer: CUSTOMER,
      data: { expires_at: new Date(Date.now() + 86400000).toISOString(), given_by: "Ada", code_id: "c1" },
    },
  ];
  await call(environment, await syncRequest(environment, "POST", "/_sync/records", { upsert: records, delete: [] }));
  const signedIn = await call(environment, browser("POST", "/portal/sign-in/access-code/", { form: { code: "ABCD1234EFGH" } }));
  return { environment, cookie: cookieFrom(signedIn) };
}

async function inboxOf(environment, kind) {
  const response = await call(environment, await syncRequest(environment, "GET", "/_sync/inbox?after=0&limit=100"));
  return (await response.json()).events.filter((e) => e.kind === kind);
}

test("the quote shows the choices, and the total follows what's picked", async () => {
  const { environment, cookie } = await shop();
  let page = await (await call(environment, browser("GET", `/portal/quotes/${QUOTE}/`, { cookie }))).text();
  assert.match(page, /Total with these choices: <strong>\$1,800.00<\/strong>, with a deposit of \$900.00/);
  assert.match(page, /value="01900000-0000-7000-8000-0000000000a1" checked/);
  assert.match(page, /src="\/portal\/static\/signature-abc.js"/);
  assert.match(page, /Approve quote for \$1,800.00/);
  page = await (
    await call(environment, browser("GET", `/portal/quotes/${QUOTE}/?pick-1=${MAPLE}&pick=${BENCH}`, { cookie }))
  ).text();
  assert.match(page, /\$2,400.00<\/strong>, with a deposit of \$1,200.00/);
  assert.match(page, /Approve quote for \$2,400.00/);
  // A combination Trammel didn't work out: the total follows approval.
  page = await (await call(environment, browser("GET", `/portal/quotes/${QUOTE}/?pick=${BENCH}`, { cookie }))).text();
  assert.match(page, /We'll confirm the total with your choices/);
});

test("approving checks the name, the terms and the signature, then waits on Trammel", async () => {
  const { environment, cookie } = await shop();
  const approve = (form) =>
    call(environment, browser("POST", `/portal/quotes/${QUOTE}/respond/`, { cookie, form: { answer: "approve", ...form } }));
  let response = await approve({ name: "", terms: "abc123", accept: "1", signature: PNG });
  assert.match(response.headers.get("Location"), /notice=answer-name/);
  response = await approve({ name: "Dana", terms: "old", accept: "1", signature: PNG });
  assert.match(response.headers.get("Location"), /notice=answer-terms-changed/);
  response = await approve({ name: "Dana", terms: "abc123", signature: PNG });
  assert.match(response.headers.get("Location"), /notice=answer-accept/);
  response = await approve({ name: "Dana", terms: "abc123", accept: "1", signature: "" });
  assert.match(response.headers.get("Location"), /notice=answer-sign/);
  assert.equal((await inboxOf(environment, "quote_response")).length, 0);

  const form = new URLSearchParams({ answer: "approve", name: "Dana Price", terms: "abc123", accept: "1", signature: PNG, comment: "Thanks" });
  form.append("pick", MAPLE);
  form.append("pick", BENCH);
  response = await call(
    environment,
    new Request(`https://portal.example.com/portal/quotes/${QUOTE}/respond/`, {
      method: "POST",
      headers: { Cookie: cookie, Origin: "https://portal.example.com", "Content-Type": "application/x-www-form-urlencoded", "CF-Connecting-IP": "203.0.113.9" },
      body: form.toString(),
    }),
  );
  assert.match(response.headers.get("Location"), /notice=answer-received/);
  const [answer] = await inboxOf(environment, "quote_response");
  assert.deepEqual(
    { ...answer.data, signature: answer.data.signature.slice(0, 22) },
    {
      revision: QUOTE, answer: "approve", comment: "Thanks", email: "", how: "signed in with a code from Ada",
      given_by: "Ada", name: "Dana Price", terms: "abc123", picks: [MAPLE, BENCH], signature: "data:image/png;base64,",
    },
  );
  const page = await (await call(environment, browser("GET", `/portal/quotes/${QUOTE}/`, { cookie }))).text();
  assert.match(page, /We've received your approval/);
  assert.doesNotMatch(page, /<form method="post" action="\/portal\/quotes/);
  // Answered once: a second answer is refused.
  response = await call(environment, browser("POST", `/portal/quotes/${QUOTE}/respond/`, { cookie, form: { answer: "decline" } }));
  assert.match(response.headers.get("Location"), /notice=answer-closed/);
});

test("a problem from Trammel shows on the quote; text in a link never does", async () => {
  const { environment, cookie } = await shop();
  await call(environment, await syncRequest(environment, "POST", "/_sync/records", {
    upsert: [{ type: "quote", id: QUOTE, customer: CUSTOMER, job: JOB, sort: "1", data: {
      kind: "Quote", noun: "quote", number: "Q-1", title: "Table", total: "$1,800.00", status: "Sent", open: false,
      problem: "This quote was revised before your answer arrived. Please look at the new one.",
    } }],
    delete: [],
  }));
  const page = await (await call(environment, browser("GET", `/portal/quotes/${QUOTE}/?notice=<b>Pay+now</b>`, { cookie }))).text();
  assert.match(page, /was revised before your answer arrived/);
  assert.doesNotMatch(page, /Pay now/);
});

test("declining needs no signature", async () => {
  const { environment, cookie } = await shop();
  const response = await call(environment, browser("POST", `/portal/quotes/${QUOTE}/respond/`, { cookie, form: { answer: "decline", comment: "Too dear" } }));
  assert.match(response.headers.get("Location"), /answer-received/);
  const [answer] = await inboxOf(environment, "quote_response");
  assert.equal(answer.data.answer, "decline");
  assert.equal(answer.data.comment, "Too dear");
});

test("files sent are stored, listed as on their way, and passed on", async () => {
  const { environment, cookie } = await shop();
  const send = (files, job = JOB) => {
    const body = new FormData();
    body.append("job", job);
    body.append("note", "The sketch");
    for (const f of files) body.append("files", f);
    return call(environment, new Request("https://portal.example.com/portal/send/", {
      method: "POST",
      headers: { Cookie: cookie, Origin: "https://portal.example.com", "CF-Connecting-IP": "203.0.113.9" },
      body,
    }));
  };
  let response = await send([new File(["x".repeat(2 * 1024 * 1024)], "big.png", { type: "image/png" })]);
  assert.match(response.headers.get("Location"), /notice=send-too-big/);
  response = await send([new File(["a"], "a.png"), new File(["b"], "b.png"), new File(["c"], "c.png")]);
  assert.match(response.headers.get("Location"), /notice=send-too-many/);
  response = await send([new File(["0 SECTION"], "../../etc/Screen Door (v2).dxf", { type: "application/dxf" })]);
  assert.equal(response.headers.get("Location"), `/portal/jobs/${JOB}/?notice=files-sent`);
  const [sent] = await inboxOf(environment, "upload");
  assert.equal(sent.data.name, "Screen Door _v2_.dxf");
  // Stored by its id: a name with spaces never has to pass as a key.
  assert.equal(sent.data.key, `upload/${sent.data.id}`);
  assert.equal(sent.data.job, JOB);
  assert.equal(sent.data.sent_by, "Foundry Brewing");
  assert.ok(environment.FILES.store.has(sent.data.key));
  const page = await (await call(environment, browser("GET", `/portal/jobs/${JOB}/`, { cookie }))).text();
  assert.match(page, /Screen Door _v2_.dxf/);
  assert.match(page, /Sending to us/);
  // Trammel fetches it, then removes it.
  const fetched = await call(environment, await syncRequest(environment, "GET", `/_sync/objects/${sent.data.key}`));
  assert.equal(await fetched.text(), "0 SECTION");
});

test("a file not yet collected can be removed; one Trammel has can't", async () => {
  const { environment, cookie } = await shop();
  const body = new FormData();
  body.append("job", JOB);
  body.append("files", new File(["0 SECTION"], "Screen Door.dxf", { type: "application/dxf" }));
  await call(environment, new Request("https://portal.example.com/portal/send/", {
    method: "POST",
    headers: { Cookie: cookie, Origin: "https://portal.example.com", "CF-Connecting-IP": "203.0.113.9" },
    body,
  }));
  const [sent] = await inboxOf(environment, "upload");
  // One kept the old way, before 0.9.4, stuck with its name in the key.
  const OLD = "01900000-0000-7000-8000-0000000000c1";
  await environment.FILES.put(`upload/${OLD}/Old Drawing.pdf`, "%PDF");
  await call(environment, await syncRequest(environment, "POST", "/_sync/records", {
    upsert: [
      { type: "upload", id: OLD, customer: CUSTOMER, job: JOB, sort: "1", data: { name: "Old Drawing.pdf", status: "Sending to us…" } },
      { type: "upload", id: "01900000-0000-7000-8000-0000000000c2", customer: CUSTOMER, job: JOB, sort: "2", data: { name: "Taken.pdf", status: "Waiting for us to look at it" } },
    ],
    delete: [],
  }));
  let page = await (await call(environment, browser("GET", `/portal/jobs/${JOB}/`, { cookie }))).text();
  assert.equal((page.match(/action="\/portal\/send\/remove\/"/g) || []).length, 2);
  const remove = (id) => call(environment, browser("POST", "/portal/send/remove/", { cookie, form: { id } }));

  let response = await remove(sent.data.id);
  assert.equal(response.headers.get("Location"), `/portal/jobs/${JOB}/?notice=file-removed`);
  assert.equal((await inboxOf(environment, "upload")).length, 0);
  assert.ok(!environment.FILES.store.has(sent.data.key));
  response = await remove(OLD);
  assert.match(response.headers.get("Location"), /notice=file-removed/);
  assert.ok(!environment.FILES.store.has(`upload/${OLD}/Old Drawing.pdf`));
  page = await (await call(environment, browser("GET", `/portal/jobs/${JOB}/?notice=file-removed`, { cookie }))).text();
  assert.doesNotMatch(page, /Screen Door|Old Drawing/);
  assert.match(page, /The file is removed/);
  // What reached Trammel stays.
  response = await remove("01900000-0000-7000-8000-0000000000c2");
  assert.match(response.headers.get("Location"), /notice=file-not-removed/);
  assert.match(await (await call(environment, browser("GET", `/portal/jobs/${JOB}/`, { cookie }))).text(), /Taken.pdf/);
});

test("an invoice with a payment link can be paid online", async () => {
  const { environment, cookie } = await shop();
  const INVOICE = "01900000-0000-7000-8000-000000000030";
  const OTHER = "01900000-0000-7000-8000-000000000031";
  const invoice = (id, data) => ({
    type: "invoice", id, customer: CUSTOMER, job: JOB, sort: "1",
    data: { kind: "Invoice", number: id.slice(-2), total: "$95.00", balance: "$95.00", balance_due: true, status: "Issued", ...data },
  });
  await call(environment, await syncRequest(environment, "POST", "/_sync/records", {
    upsert: [
      invoice(INVOICE, { pay_url: "https://buy.stripe.com/test_1" }),
      // Never anything but an https address, whatever is published.
      invoice(OTHER, { pay_url: "javascript:alert(1)" }),
    ],
    delete: [],
  }));
  let page = await (await call(environment, browser("GET", `/portal/invoices/${INVOICE}/`, { cookie }))).text();
  assert.match(page, /<a class="button primary" href="https:\/\/buy.stripe.com\/test_1" rel="noopener">Pay \$95.00 online<\/a>/);
  page = await (await call(environment, browser("GET", `/portal/invoices/${OTHER}/`, { cookie }))).text();
  assert.doesNotMatch(page, /Pay \$95.00 online|javascript:/);
  const home = await (await call(environment, browser("GET", "/portal/", { cookie }))).text();
  assert.match(home, /Pay online: <a href="https:\/\/buy.stripe.com\/test_1" rel="noopener">Card<\/a>/);
  page = await (await call(environment, browser("GET", `/portal/invoices/${INVOICE}/?notice=payment-received`, { cookie }))).text();
  assert.match(page, /Your payment is being confirmed/);
  // Paid: no button.
  await call(environment, await syncRequest(environment, "POST", "/_sync/records", {
    upsert: [invoice(INVOICE, { balance: "$0.00", balance_due: false, status: "Paid", pay_url: "" })],
    delete: [],
  }));
  page = await (await call(environment, browser("GET", `/portal/invoices/${INVOICE}/`, { cookie }))).text();
  assert.doesNotMatch(page, /online<\/a>/);
});

test("each way to pay gets its own button, https only", async () => {
  const { environment, cookie } = await shop();
  const INVOICE = "01900000-0000-7000-8000-000000000032";
  await call(environment, await syncRequest(environment, "POST", "/_sync/records", {
    upsert: [{
      type: "invoice", id: INVOICE, customer: CUSTOMER, job: JOB, sort: "1",
      data: {
        kind: "Invoice", number: "INV-9", total: "$95.00", balance: "$95.00", balance_due: true, status: "Issued",
        pay_url: "https://buy.stripe.com/test_1",
        pay: [
          { label: "Pay $95.00 online", name: "Card", url: "https://buy.stripe.com/test_1" },
          { label: "Pay $95.00 with PayPal", name: "PayPal", url: "https://www.paypal.com/invoice/p/#INV2-1" },
          { label: "Pay elsewhere", name: "Elsewhere", url: "javascript:alert(1)" },
        ],
      },
    }],
    delete: [],
  }));
  const page = await (await call(environment, browser("GET", `/portal/invoices/${INVOICE}/`, { cookie }))).text();
  assert.match(page, /href="https:\/\/buy.stripe.com\/test_1" rel="noopener">Pay \$95.00 online<\/a>/);
  assert.match(page, /href="https:\/\/www.paypal.com\/invoice\/p\/#INV2-1" rel="noopener">Pay \$95.00 with PayPal<\/a>/);
  assert.doesNotMatch(page, /Pay elsewhere|javascript:/);
  const home = await (await call(environment, browser("GET", "/portal/", { cookie }))).text();
  assert.match(home, /Pay online: <a href="https:\/\/buy.stripe.com\/test_1" rel="noopener">Card<\/a> · <a href="https:\/\/www.paypal.com\/invoice\/p\/#INV2-1" rel="noopener">PayPal<\/a>/);
});

test("store webhooks wait in the inbox; sign-in callbacks go on to Trammel", async () => {
  const { environment } = await shop({ hooks: { shopify: true, etsy: true, ebay: true, ebay_deletion: true } });
  let response = await call(environment, new Request("https://portal.example.com/imports/shopify/webhook/", {
    method: "POST",
    headers: { "X-Shopify-Hmac-Sha256": "sig==", "X-Shopify-Shop-Domain": "shop.myshopify.com", "X-Shopify-Topic": "orders/create" },
    body: '{"id": 1}',
  }));
  assert.equal(response.status, 200);
  const [hook] = await inboxOf(environment, "shopify_webhook");
  assert.deepEqual(hook.data, { body: '{"id": 1}', hmac: "sig==", shop: "shop.myshopify.com", topic: "orders/create" });

  response = await call(environment, new Request("https://portal.example.com/imports/etsy/callback/?code=abc&state=xyz"));
  assert.equal(response.status, 302);
  assert.equal(response.headers.get("Location"), "https://trammel.home.arpa:8727/imports/etsy/callback/?code=abc&state=xyz");

  response = await call(environment, new Request("https://portal.example.com/imports/ebay/deletion/?challenge_code=c0de"));
  const expected = await sha256Hex("c0de" + "verify-token-123" + "https://portal.example.com/imports/ebay/deletion/");
  assert.deepEqual(await response.json(), { challengeResponse: expected });
  response = await call(environment, new Request("https://portal.example.com/imports/ebay/deletion/", {
    method: "POST", headers: { "X-EBAY-SIGNATURE": "eyJ..." }, body: '{"metadata":{}}',
  }));
  assert.equal(response.status, 204);
  const [notice] = await inboxOf(environment, "ebay_deletion");
  assert.equal(notice.data.signature, "eyJ...");
});

test("a store connection that's off isn't there", async () => {
  const { environment } = await shop({ hooks: {} });
  for (const [method, path] of [["POST", "/imports/shopify/webhook/"], ["GET", "/imports/etsy/callback/"], ["GET", "/imports/ebay/deletion/"]]) {
    const response = await call(environment, new Request(`https://portal.example.com${path}`, { method, body: method === "POST" ? "{}" : undefined }));
    assert.equal(response.status, 404, path);
  }
});
