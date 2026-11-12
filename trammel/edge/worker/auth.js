// Signing in to the edge portal (FR-PRT-1, -6, -10): a six-digit code emailed by
// the Worker, or a code from staff; then a session cookie. The same rules as
// Trammel's own portal: codes expire in 10 minutes, die after 5 wrong tries, and
// asking and trying are rate-limited by address and by IP address.

import { accessHash, codeHash, ipKey, normalizeAccessCode, same, sha256Hex, sixDigits, token } from "./crypto.js";
import { earlier, inbox, later, now, record } from "./db.js";
import { sendMail } from "./smtp.js";

export const COOKIE = "__Host-portal";
const SESSION_MS = 12 * 3600 * 1000;
const CODE_MS = 10 * 60 * 1000;
const MAX_TRIES = 5;
const CODES_PER_ADDRESS = 3; // in 15 minutes
const REQUESTS_PER_IP = 10; // in an hour
const FAILURES_PER_IP = 20; // in an hour
const FAILURES_PER_ADDRESS = 10; // in a day

export function normalizeEmail(email) {
  return String(email || "").trim().toLowerCase().slice(0, 254);
}

export function clientIp(request) {
  return request.headers.get("CF-Connecting-IP") || "";
}

async function count(db, sql, ...args) {
  const row = await db.prepare(sql).bind(...args).first();
  return row ? Number(row.n) : 0;
}

async function attempt(db, kind, email, ip, ok = false) {
  await db
    .prepare("INSERT INTO attempts (at, kind, email, ip_key, ok) VALUES (?, ?, ?, ?, ?)")
    .bind(now(), kind, email, ipKey(ip), ok ? 1 : 0)
    .run();
}

async function failures(db, field, value, sinceMs) {
  return count(
    db,
    `SELECT COUNT(*) AS n FROM attempts WHERE kind = 'verify' AND ok = 0 AND ${field} = ? AND at >= ?`,
    value,
    earlier(sinceMs),
  );
}

async function addressLocked(db, email) {
  return (await failures(db, "email", email, 24 * 3600 * 1000)) >= FAILURES_PER_ADDRESS;
}

async function ipLocked(db, ip) {
  const key = ipKey(ip);
  return Boolean(key) && (await failures(db, "ip_key", key, 3600 * 1000)) >= FAILURES_PER_IP;
}

/** The customers an address may sign in for: theirs or a contact's, with
 * access on (Trammel publishes only customers whose access is on). */
export async function customersFor(db, email) {
  if (!email) return [];
  const { results } = await db
    .prepare("SELECT id, data FROM records WHERE type = 'customer'")
    .all();
  return (results || [])
    .map((row) => ({ id: row.id, ...JSON.parse(row.data) }))
    .filter((c) => (c.signers || []).some((s) => s.email === email))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** Email a code if the address may sign in and nothing is limited. Says nothing
 * either way, so addresses can't be probed. Returns false only when the email
 * couldn't be sent. */
export async function requestCode(env, site, email, ip, { mailer = sendMail } = {}) {
  const db = env.DB;
  email = normalizeEmail(email);
  if (!email) return true;
  const tooMany =
    (await count(
      db,
      "SELECT COUNT(*) AS n FROM attempts WHERE kind = 'request' AND email = ? AND at >= ?",
      email,
      earlier(15 * 60 * 1000),
    )) >= CODES_PER_ADDRESS ||
    (Boolean(ipKey(ip)) &&
      (await count(
        db,
        "SELECT COUNT(*) AS n FROM attempts WHERE kind = 'request' AND ip_key = ? AND at >= ?",
        ipKey(ip),
        earlier(3600 * 1000),
      )) >= REQUESTS_PER_IP) ||
    (await addressLocked(db, email));
  await attempt(db, "request", email, ip);
  if (tooMany) return true;
  const customers = await customersFor(db, email);
  if (!customers.length || !site?.email?.enabled) return true;
  const code = sixDigits();
  const fill = (text) =>
    String(text || "")
      .replaceAll("%%CODE%%", `${code.slice(0, 3)} ${code.slice(3)}`)
      .replaceAll("%%MINUTES%%", "10");
  try {
    await mailer(JSON.parse(env.SMTP || "{}"), {
      to: email,
      subject: fill(site.email.subject),
      text: fill(site.email.body),
    });
  } catch (error) {
    await inbox(db, "mail_failed", { email, error: String(error.message || error) }, { ip });
    return false;
  }
  await db
    .prepare(
      "INSERT INTO codes (id, email, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
    )
    .bind(crypto.randomUUID(), email, await codeHash(env, email, code), now(), later(CODE_MS))
    .run();
  await inbox(db, "code_sent", { email, customer: customers[0].id }, { ip, customer: customers[0].id });
  return true;
}

/** The customers `email` may sign in for if `code` is one of its waiting codes,
 * which is then used up; otherwise an empty list. Every try counts against each
 * waiting code before any is compared. */
export async function verifyCode(env, email, code, ip) {
  const db = env.DB;
  email = normalizeEmail(email);
  code = String(code || "").replace(/\D/g, "");
  if ((await ipLocked(db, ip)) || (await addressLocked(db, email))) {
    await attempt(db, "verify", email, ip);
    return [];
  }
  const { results } = await db
    .prepare(
      "SELECT id, code_hash FROM codes WHERE email = ? AND used_at IS NULL AND expires_at > ? AND tries < ?",
    )
    .bind(email, now(), MAX_TRIES)
    .all();
  const live = results || [];
  if (live.length) {
    await db.batch(
      live.map((row) => db.prepare("UPDATE codes SET tries = tries + 1 WHERE id = ?").bind(row.id)),
    );
  }
  let match = null;
  if (code.length === 6) {
    const wanted = await codeHash(env, email, code);
    match = live.find((row) => same(row.code_hash, wanted)) || null;
  }
  if (!match) {
    await attempt(db, "verify", email, ip);
    return [];
  }
  const used = await db
    .prepare("UPDATE codes SET used_at = ? WHERE id = ? AND used_at IS NULL")
    .bind(now(), match.id)
    .run();
  const customers = used.meta?.changes ? await customersFor(db, email) : [];
  await attempt(db, "verify", email, ip, customers.length > 0);
  return customers;
}

/** Sign in with a code from staff; the customer, or null. */
export async function useAccessCode(env, code, ip) {
  const db = env.DB;
  code = normalizeAccessCode(code);
  if (await ipLocked(db, ip)) {
    await attempt(db, "verify", "", ip);
    return null;
  }
  let found = null;
  if (code.length === 12) {
    const hash = await accessHash(env, code);
    const row = await record(db, "access", hash);
    if (row && row.data.expires_at > now()) {
      // Claimed in one statement, so the same code can't sign in twice at once.
      const claimed = await db
        .prepare("INSERT OR IGNORE INTO used_access_codes (hash, at) VALUES (?, ?)")
        .bind(hash, now())
        .run();
      if (claimed.meta?.changes) {
        const customer = await record(db, "customer", row.customer);
        if (customer) found = { id: customer.id, ...customer.data, givenBy: row.data.given_by, code: row.data.code_id };
      }
    }
  }
  await attempt(db, "verify", "", ip, Boolean(found));
  return found;
}

// --------------------------------------------------------------------------
// Sessions
// --------------------------------------------------------------------------

function cookieValue(request, name) {
  const header = request.headers.get("Cookie") || "";
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return rest.join("=");
  }
  return "";
}

export function sessionCookie(value, maxAge) {
  return `${COOKIE}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${maxAge}`;
}

/** Start a session; returns the Set-Cookie header. With several customers to
 * choose from, the session waits on the choice. */
export async function startSession(env, { customers, email = "", givenBy = "" }) {
  const raw = token();
  const one = customers.length === 1 ? customers[0] : null;
  await env.DB.prepare(
    "INSERT INTO sessions (id, customer, email, name, given_by, choices, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  )
    .bind(
      await sha256Hex(raw),
      one ? one.id : null,
      email,
      one ? nameAt(one, email) : "",
      givenBy,
      JSON.stringify(customers.map((c) => c.id)),
      now(),
      later(SESSION_MS),
    )
    .run();
  return sessionCookie(raw, SESSION_MS / 1000);
}

export function nameAt(customer, email) {
  const signer = (customer.signers || []).find((s) => s.email === email);
  return signer ? signer.name : customer.name;
}

/** The signed-in visitor, checked afresh: the session not expired, the customer
 * still published (access on), and the address still one that may sign in. */
export async function visitor(env, request) {
  const raw = cookieValue(request, COOKIE);
  if (!raw) return null;
  const id = await sha256Hex(raw);
  const row = await env.DB.prepare("SELECT * FROM sessions WHERE id = ? AND expires_at > ?")
    .bind(id, now())
    .first();
  if (!row) return null;
  const session = { ...row, sessionId: id, choices: JSON.parse(row.choices || "[]") };
  if (!row.customer) return { pending: true, ...session };
  const customer = await record(env.DB, "customer", row.customer);
  if (!customer) return null;
  if (!row.given_by && !(customer.data.signers || []).some((s) => s.email === row.email)) return null;
  return {
    ...session,
    customerId: customer.id,
    customerName: customer.data.name,
    name: row.name || customer.data.name,
    how: row.given_by ? `signed in with a code from ${row.given_by}` : `signed in as ${row.email}`,
  };
}

export async function choose(env, session, customerId) {
  if (!session.choices.includes(customerId)) return null;
  const customer = await record(env.DB, "customer", customerId);
  if (!customer) return null;
  const name = nameAt({ id: customer.id, ...customer.data }, session.email);
  await env.DB.prepare("UPDATE sessions SET customer = ?, name = ? WHERE id = ?")
    .bind(customerId, name, session.sessionId)
    .run();
  return { id: customer.id, name: customer.data.name, person: name };
}

export async function endSession(env, request) {
  const raw = cookieValue(request, COOKIE);
  if (raw) await env.DB.prepare("DELETE FROM sessions WHERE id = ?").bind(await sha256Hex(raw)).run();
  return sessionCookie("", 0);
}
