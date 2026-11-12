// The customer's pages (FR-PRT-2 to -4): what Trammel published for them, and
// nothing else. Every record a page shows is checked to be the visitor's own.

import {
  choose,
  clientIp,
  endSession,
  noteAttempt,
  passedBotCheck,
  recentFrom,
  normalizeEmail,
  requestCode,
  startSession,
  useAccessCode,
  verifyCode,
  visitor as currentVisitor,
} from "./auth.js";
import { inbox, record, records } from "./db.js";
import { DOCUMENT_HEADERS, botCheck, html, page, redirect, respond } from "./html.js";
import { picksFrom, respondSection, takeAnswer } from "./respond.js";
import { removeSent, sendSection, takeFiles } from "./send.js";

const ID = "[0-9a-fA-F-]{36}";
const SENDS_PER_HOUR = 20;

function form(request) {
  return request.formData();
}

function notFound(site, visitor) {
  return respond(
    page({
      site,
      visitor,
      title: "Not found",
      body: html`<h1>Not found</h1><p>That page isn't here. <a href="/portal/">Back to your account</a>.</p>`,
    }),
    { status: 404 },
  );
}

async function object(env, key, { download = "", headers = {} } = {}) {
  const found = key ? await env.FILES.get(key) : null;
  if (!found) return null;
  const extra = download
    ? { "Content-Disposition": `attachment; filename="${download.replace(/["\\\r\n]/g, "_")}"` }
    : {};
  return new Response(found.body, {
    headers: {
      "Content-Type": found.httpMetadata?.contentType || "application/octet-stream",
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "private, no-store",
      ...extra,
      ...headers,
    },
  });
}

/** The visitor's own record of a type, or null. */
async function own(env, visitor, type, id) {
  const row = await record(env.DB, type, id);
  return row && row.customer === visitor.customerId ? row : null;
}

function waiting(quotes) {
  return quotes.filter((q) => q.data.open);
}

function jobsTable(jobs) {
  if (!jobs.length) return html`<p class="muted">No jobs yet.</p>`;
  return html`<div class="table-wrap"><table>
    <thead><tr><th scope="col">Job</th><th scope="col">Status</th><th scope="col">Promised</th></tr></thead>
    <tbody>${jobs.map(
      (j) => html`<tr><td><a href="/portal/jobs/${j.id}/">${j.data.number} · ${j.data.title}</a></td><td>${j.data.status}</td><td>${j.data.promised || "—"}</td></tr>`,
    )}</tbody></table></div>`;
}

function filesList(files) {
  return html`<ul class="plain portal-files">${files.map(
    (f) => html`<li>${f.data.thumb ? html`<img src="/portal/files/${f.id}/thumbnail/" alt="" class="portal-thumb">` : ""}
      <a href="/portal/files/${f.id}/">${f.data.title}</a> <span class="muted small">${f.data.name}${f.data.size ? html` · ${f.data.size}` : ""}</span></li>`,
  )}</ul>`;
}

function quoteList(quotes) {
  return html`<ul class="plain portal-list">${quotes.map(
    (q) => html`<li><a href="/portal/quotes/${q.id}/"><strong>${q.data.kind} ${q.data.number}</strong> · ${q.data.title}</a> — ${q.data.total} · ${q.data.status}</li>`,
  )}</ul>`;
}

function invoiceList(invoices) {
  return html`<ul class="plain portal-list">${invoices.map(
    (i) => html`<li><a href="/portal/invoices/${i.id}/"><strong>${i.data.kind} ${i.data.number}</strong></a> — ${i.data.total}${i.data.balance_due ? html`, ${i.data.balance} to pay${i.data.due ? html`, due ${i.data.due}` : ""}` : html` · ${i.data.status}`}</li>`,
  )}</ul>`;
}

async function home(env, site, visitor, notice) {
  const db = env.DB;
  const customer = visitor.customerId;
  const [jobs, quotes, invoices, files, sent] = await Promise.all([
    records(db, "job", { customer }),
    records(db, "quote", { customer }),
    records(db, "invoice", { customer }),
    records(db, "file", { customer }),
    records(db, "upload", { customer }),
  ]);
  const open = waiting(quotes);
  const unpaid = invoices.filter((i) => i.data.balance_due);
  const accountFiles = files.filter((f) => !f.job);
  return page({
    site,
    visitor,
    notice,
    title: "Your account",
    body: html`<h1>Hello, ${visitor.name}</h1>
${
  open.length || unpaid.length
    ? html`<section class="card" aria-labelledby="waiting-heading"><h2 id="waiting-heading">Waiting for you</h2>
  <ul class="plain portal-list">
  ${open.map((q) => html`<li><a href="/portal/quotes/${q.id}/"><strong>${q.data.kind} ${q.data.number}</strong> · ${q.data.title}</a> — ${q.data.total}${q.data.valid_until ? html`, valid until ${q.data.valid_until}` : ""}. <span class="muted">Ready for you to approve or decline.</span></li>`)}
  ${unpaid.map((i) => html`<li><a href="/portal/invoices/${i.id}/"><strong>${i.data.kind} ${i.data.number}</strong></a> — ${i.data.balance} to pay${i.data.due ? html`, due ${i.data.due}` : ""}.${payOptions(i.data).length ? html` Pay online:${payOptions(i.data).map((o, n) => html` <a href="${o.url}" rel="noopener">${o.name}</a>${n < payOptions(i.data).length - 1 ? " ·" : ""}`)}` : ""}</li>`)}
  </ul></section>`
    : ""
}
<section aria-labelledby="jobs-heading"><h2 id="jobs-heading">Your jobs</h2>${jobsTable(jobs)}</section>
${accountFiles.length ? html`<section aria-labelledby="files-heading"><h2 id="files-heading">Files</h2>${filesList(accountFiles)}</section>` : ""}
${sendSection(site, sent.filter((s) => !s.job), null)}`,
  });
}

async function job(env, site, visitor, id, notice) {
  const found = await own(env, visitor, "job", id);
  if (!found) return null;
  const db = env.DB;
  const [quotes, invoices, agreements, files, notes, sent] = await Promise.all([
    records(db, "quote", { job: id }),
    records(db, "invoice", { job: id }),
    records(db, "agreement", { job: id }),
    records(db, "file", { job: id }),
    records(db, "note", { job: id }),
    records(db, "upload", { job: id }),
  ]);
  const j = found.data;
  return page({
    site,
    visitor,
    notice,
    title: `${j.number} ${j.title}`,
    body: html`<p class="breadcrumb"><a href="/portal/">Your account</a></p>
<h1>${j.number} · ${j.title}</h1>
<dl class="facts-inline"><div><dt>Status</dt><dd>${j.status}</dd></div>${j.promised ? html`<div><dt>Promised</dt><dd>${j.promised}</dd></div>` : ""}${j.due ? html`<div><dt>Due</dt><dd>${j.due}</dd></div>` : ""}</dl>
${quotes.length ? html`<section><h2>Quotes</h2>${quoteList(quotes)}</section>` : ""}
${invoices.length ? html`<section><h2>Invoices</h2>${invoiceList(invoices)}</section>` : ""}
${agreements.length ? html`<section><h2>Agreements</h2><ul class="plain">${agreements.map((a) => html`<li>${a.data.title}, accepted ${a.data.accepted}${a.data.pdf ? html` · <a href="/portal/agreements/${a.id}/pdf/">PDF</a>` : ""}</li>`)}</ul></section>` : ""}
${notes.length ? html`<section><h2>Updates</h2><ul class="plain portal-updates">${notes.map((n) => html`<li><span class="muted small">${n.data.at}${n.data.author ? html` · ${n.data.author}` : ""}</span><p class="prewrap">${n.data.text}</p>${n.data.photo ? html`<a href="/portal/notes/${n.id}/photo/"><img src="/portal/notes/${n.id}/photo/" alt="" class="portal-thumb"></a>` : ""}</li>`)}</ul></section>` : ""}
${files.length ? html`<section><h2>Files</h2>${filesList(files)}</section>` : ""}
${sendSection(site, sent, id)}`,
  });
}

function documentFrame(src, title) {
  return html`<iframe class="document-preview" src="${src}" title="${title}" sandbox></iframe>`;
}

async function quote(env, site, visitor, id, notice, picks) {
  const found = await own(env, visitor, "quote", id);
  if (!found) return null;
  const q = found.data;
  return page({
    site,
    visitor,
    notice,
    title: `${q.kind} ${q.number}`,
    body: html`<p class="breadcrumb"><a href="/portal/">Your account</a>${found.job ? html` · <a href="/portal/jobs/${found.job}/">${q.job_number}</a>` : ""}</p>
<h1>${q.kind} ${q.number} · ${q.title}</h1>
<dl class="facts-inline"><div><dt>Total</dt><dd><strong>${q.total}</strong></dd></div><div><dt>Status</dt><dd>${q.status}</dd></div>${q.valid_until ? html`<div><dt>Valid until</dt><dd>${q.valid_until}</dd></div>` : ""}</dl>
${q.approval ? html`<p class="card">Approved by ${q.approval.by} on ${q.approval.at}.</p>` : ""}
<p class="actions">${q.pdf ? html`<a class="button" href="/portal/quotes/${found.id}/pdf/">Download PDF</a>` : ""}</p>
${q.doc ? documentFrame(`/portal/quotes/${found.id}/document/`, `${q.kind} ${q.number}`) : ""}
${q.respond && q.respond.choices && (q.respond.choices.groups.length || q.respond.choices.options.length) ? html`<p class="muted small">The document shows the standard choices; yours are in the total below.</p>` : ""}
${respondSection(site, visitor, found, picks)}`,
  });
}

/** The ways an invoice can be paid online, as Trammel published them (https
 * only): [{label, name, url}]. One published before 0.10.1 has only pay_url. */
function payOptions(i) {
  if (!i.balance_due) return [];
  const given = Array.isArray(i.pay)
    ? i.pay
    : i.pay_url
      ? [{ label: `Pay ${i.balance} online`, name: "Card", url: i.pay_url }]
      : [];
  return given.filter((o) => o && typeof o.url === "string" && o.url.startsWith("https://"));
}

async function invoice(env, site, visitor, id, notice) {
  const found = await own(env, visitor, "invoice", id);
  if (!found) return null;
  const i = found.data;
  return page({
    site,
    visitor,
    notice,
    title: `${i.kind} ${i.number}`,
    body: html`<p class="breadcrumb"><a href="/portal/">Your account</a>${found.job ? html` · <a href="/portal/jobs/${found.job}/">${i.job_number}</a>` : ""}</p>
<h1>${i.kind} ${i.number}</h1>
<dl class="facts-inline"><div><dt>Total</dt><dd><strong>${i.total}</strong></dd></div><div><dt>Status</dt><dd>${i.status}</dd></div>${i.balance_due ? html`<div><dt>To pay</dt><dd>${i.balance}</dd></div>` : ""}${i.due ? html`<div><dt>Due</dt><dd>${i.due}</dd></div>` : ""}</dl>
<p class="actions">${payOptions(i).map((o) => html`<a class="button primary" href="${o.url}" rel="noopener">${o.label}</a> `)}${i.pdf ? html`<a class="button" href="/portal/invoices/${found.id}/pdf/">Download PDF</a>` : ""}</p>
${i.doc ? documentFrame(`/portal/invoices/${found.id}/document/`, `${i.kind} ${i.number}`) : ""}`,
  });
}

// --------------------------------------------------------------------------
// Signing in
// --------------------------------------------------------------------------

function signInPage(site, notice) {
  const ready = Boolean(site?.email?.enabled);
  return page({
    site,
    notice,
    title: "Sign in",
    body: html`<section class="card portal-narrow"><h1>Sign in</h1>
${
  ready
    ? html`<p>See your jobs, quotes and invoices with ${site.business.name}. Enter your email address and we'll send you a code to sign in with. No password needed.</p>
  <form method="post" class="stack">
    <div class="field"><label for="portal-email">Email address</label>
      <input type="email" id="portal-email" name="email" autocomplete="email" required autofocus></div>
    ${botCheck(site)}
    <div class="actions"><button type="submit" class="primary">Send me a code</button></div>
  </form>
  <p class="muted small">Have a sign-in code from us? <a href="/portal/sign-in/access-code/">Enter it here</a>.</p>`
    : html`<p>See your jobs, quotes and invoices with ${site?.business?.name || "us"}. Sign in with the code we gave you, or contact us for one.</p>
  <p class="actions"><a class="button primary" href="/portal/sign-in/access-code/">Enter your code</a></p>`
}</section>`,
  });
}

function codePage(site, email, notice) {
  return page({
    site,
    notice,
    title: "Enter your code",
    body: html`<section class="card portal-narrow"><h1>Enter your code</h1>
<p>If <strong>${email}</strong> can sign in here, a six-digit code is on its way to it. It expires in 10 minutes.</p>
<form method="post" class="stack">
  <input type="hidden" name="email" value="${email}">
  <div class="field"><label for="portal-code">Code</label>
    <input type="text" id="portal-code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7" required autofocus></div>
  <div class="actions"><button type="submit" class="primary">Sign in</button> <a href="/portal/sign-in/">Send a new code</a></div>
</form>
<p class="muted small">No email? Check your spam folder, or that the address is the one ${site?.business?.name || "we have"} has for you.</p></section>`,
  });
}

function accessPage(site, notice) {
  return page({
    site,
    notice,
    title: "Sign in with your code",
    body: html`<section class="card portal-narrow"><h1>Sign in with your code</h1>
<form method="post" class="stack">
  <div class="field"><label for="access-code">The code we gave you</label>
    <input type="text" id="access-code" name="code" autocomplete="off" autocapitalize="characters" required autofocus></div>
  ${botCheck(site)}
  <div class="actions"><button type="submit" class="primary">Sign in</button></div>
</form></section>`,
  });
}

async function choosePage(env, site, session) {
  const options = [];
  for (const id of session.choices) {
    const c = await record(env.DB, "customer", id);
    if (c) options.push(c);
  }
  return page({
    site,
    title: "Which account?",
    body: html`<section class="card portal-narrow"><h1>Which account?</h1>
<p>${session.email} can sign in for more than one customer.</p>
<form method="post" class="stack">${options.map(
      (c) => html`<button type="submit" name="customer" value="${c.id}">${c.data.name}</button>`,
    )}</form></section>`,
  });
}

function sameOrigin(request) {
  const origin = request.headers.get("Origin");
  const here = new URL(request.url).origin;
  if (origin) return origin === here;
  const referer = request.headers.get("Referer") || "";
  return referer.startsWith(`${here}/`);
}

export async function handlePortal(request, env, site) {
  const url = new URL(request.url);
  const path = url.pathname;
  const notice = url.searchParams.get("notice") || "";
  const ip = clientIp(request);
  if (request.method === "POST" && !sameOrigin(request)) {
    return respond("Forbidden", { status: 403 });
  }

  const asset = path.startsWith("/portal/static/") ? path.slice("/portal/static/".length) : "";
  if (asset && (asset === site.css || asset === site.signature_js)) {
    const found = await object(env, `assets/${asset}`, {
      headers: { "Cache-Control": "public, max-age=31536000, immutable" },
    });
    return found || new Response("Not found", { status: 404 });
  }

  const checked = { turnstile: Boolean(site.turnstile) };
  if (path === "/portal/sign-in/") {
    if (request.method === "POST") {
      const data = await form(request);
      if (!(await passedBotCheck(env, site, data, ip))) return redirect("/portal/sign-in/?notice=bot-check");
      const email = normalizeEmail(data.get("email"));
      const sent = await requestCode(env, site, email, ip);
      if (!sent) return redirect("/portal/sign-in/?notice=mail-failed");
      // The code page says it's been sent; no notice besides.
      return redirect(`/portal/sign-in/code/?email=${encodeURIComponent(email)}`);
    }
    return respond(signInPage(site, notice), checked);
  }
  if (path === "/portal/sign-in/code/") {
    if (request.method === "POST") {
      const data = await form(request);
      const email = normalizeEmail(data.get("email"));
      const customers = await verifyCode(env, email, data.get("code"), ip);
      if (!customers.length) {
        return redirect(`/portal/sign-in/code/?email=${encodeURIComponent(email)}&notice=code-wrong`);
      }
      const cookie = await startSession(env, { customers, email });
      if (customers.length === 1) {
        await inbox(env.DB, "signed_in", { email, customer: customers[0].id }, { ip, customer: customers[0].id });
        return redirect("/portal/", { "Set-Cookie": cookie });
      }
      return redirect("/portal/sign-in/choose/", { "Set-Cookie": cookie });
    }
    return respond(codePage(site, normalizeEmail(url.searchParams.get("email")), notice));
  }
  if (path === "/portal/sign-in/access-code/") {
    if (request.method === "POST") {
      const data = await form(request);
      if (!(await passedBotCheck(env, site, data, ip))) {
        return redirect("/portal/sign-in/access-code/?notice=bot-check");
      }
      const customer = await useAccessCode(env, data.get("code"), ip);
      if (!customer) return redirect("/portal/sign-in/access-code/?notice=access-wrong");
      const cookie = await startSession(env, { customers: [customer], givenBy: customer.givenBy || "staff" });
      await inbox(
        env.DB,
        "access_code_used",
        { customer: customer.id, code: customer.code || "" },
        { ip, customer: customer.id },
      );
      return redirect("/portal/", { "Set-Cookie": cookie });
    }
    return respond(accessPage(site, notice), checked);
  }
  if (path === "/portal/sign-out/" && request.method === "POST") {
    const cookie = await endSession(env, request);
    return redirect("/portal/sign-in/?notice=signed-out", { "Set-Cookie": cookie });
  }

  const visitor = await currentVisitor(env, request);
  if (!visitor) return redirect("/portal/sign-in/");
  if (visitor.pending) {
    if (path !== "/portal/sign-in/choose/") return redirect("/portal/sign-in/choose/");
    if (request.method === "POST") {
      const chosen = await choose(env, visitor, String((await form(request)).get("customer") || ""));
      if (!chosen) return redirect("/portal/sign-in/choose/");
      await inbox(env.DB, "signed_in", { email: visitor.email, customer: chosen.id }, { ip, customer: chosen.id });
      return redirect("/portal/");
    }
    return respond(await choosePage(env, site, visitor));
  }

  let match;
  if (path === "/portal/") return respond(await home(env, site, visitor, notice));
  if (path === "/portal/send/" && request.method === "POST") {
    // Sending is limited per IP address, as well as by Trammel per customer.
    if ((await recentFrom(env, "send", ip)) >= SENDS_PER_HOUR) {
      return redirect("/portal/?notice=send-too-often");
    }
    await noteAttempt(env, "send", ip);
    const data = await form(request);
    const back = data.get("job") ? `/portal/jobs/${String(data.get("job")).replace(/[^0-9a-fA-F-]/g, "")}/` : "/portal/";
    const problem = await takeFiles(env, site, visitor, data, ip, async (jobId) =>
      Boolean(await own(env, visitor, "job", jobId)),
    );
    return redirect(`${back}?notice=${problem || "files-sent"}`);
  }
  if (path === "/portal/send/remove/" && request.method === "POST") {
    const id = String((await form(request)).get("id") || "").replace(/[^0-9a-fA-F-]/g, "");
    const removed = await removeSent(env, visitor, id);
    const back = removed?.job ? `/portal/jobs/${removed.job}/` : "/portal/";
    return redirect(`${back}?notice=${removed ? "file-removed" : "file-not-removed"}`);
  }
  if ((match = path.match(new RegExp(`^/portal/quotes/(${ID})/respond/$`))) && request.method === "POST") {
    const found = await own(env, visitor, "quote", match[1]);
    if (!found) return notFound(site, visitor);
    const problem = await takeAnswer(env, visitor, found, await form(request), ip);
    return redirect(`/portal/quotes/${found.id}/?notice=${problem || "answer-received"}`);
  }
  if ((match = path.match(new RegExp(`^/portal/jobs/(${ID})/$`)))) {
    const body = await job(env, site, visitor, match[1], notice);
    return body ? respond(body) : notFound(site, visitor);
  }
  if ((match = path.match(new RegExp(`^/portal/(quotes|invoices)/(${ID})/(document/|pdf/)?$`)))) {
    const type = match[1] === "quotes" ? "quote" : "invoice";
    const found = await own(env, visitor, type, match[2]);
    if (!found) return notFound(site, visitor);
    if (match[3] === "document/") {
      return (await object(env, found.data.doc, { headers: DOCUMENT_HEADERS })) || notFound(site, visitor);
    }
    if (match[3] === "pdf/") {
      return (await object(env, found.data.pdf, { download: found.data.pdf_name })) || notFound(site, visitor);
    }
    const body =
      type === "quote"
        ? await quote(env, site, visitor, match[2], notice, picksFrom(url.searchParams))
        : await invoice(env, site, visitor, match[2], notice);
    return respond(body);
  }
  if ((match = path.match(new RegExp(`^/portal/agreements/(${ID})/pdf/$`)))) {
    const found = await own(env, visitor, "agreement", match[1]);
    return (found && (await object(env, found.data.pdf, { download: found.data.pdf_name }))) || notFound(site, visitor);
  }
  if ((match = path.match(new RegExp(`^/portal/files/(${ID})/(thumbnail/)?$`)))) {
    const found = await own(env, visitor, "file", match[1]);
    if (!found) return notFound(site, visitor);
    const response = match[2]
      ? await object(env, found.data.thumb)
      : await object(env, found.data.key, { download: found.data.name });
    return response || notFound(site, visitor);
  }
  if ((match = path.match(new RegExp(`^/portal/notes/(${ID})/photo/$`)))) {
    const found = await own(env, visitor, "note", match[1]);
    return (found && (await object(env, found.data.photo))) || notFound(site, visitor);
  }
  return notFound(site, visitor);
}
