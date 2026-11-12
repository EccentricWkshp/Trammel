// Keys, hashes and the signature on Trammel's sync calls.

const encoder = new TextEncoder();

function hex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function sha256Hex(data) {
  const bytes = typeof data === "string" ? encoder.encode(data) : data;
  return hex(await crypto.subtle.digest("SHA-256", bytes));
}

export async function hmacHex(key, message) {
  const material = await crypto.subtle.importKey(
    "raw",
    encoder.encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return hex(await crypto.subtle.sign("HMAC", material, encoder.encode(message)));
}

/** Compare two strings in time that doesn't depend on where they differ. */
export function same(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function token(bytes = 32) {
  const raw = crypto.getRandomValues(new Uint8Array(bytes));
  let text = "";
  for (const b of raw) text += String.fromCharCode(b);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A six-digit code, uniformly. */
export function sixDigits() {
  const limit = 4_294_000_000; // a multiple of 1,000,000 below 2^32
  for (;;) {
    const [n] = crypto.getRandomValues(new Uint32Array(1));
    if (n < limit) return String(n % 1_000_000).padStart(6, "0");
  }
}

/** An emailed code, as kept: keyed with the sync key, so a copy of the database
 * doesn't give the codes away. */
export function codeHash(env, email, code) {
  return hmacHex(env.SYNC_KEY, `portal:${email}:${code}`);
}

/** A code from staff (FR-PRT-10), as Trammel publishes it. */
export function accessHash(env, code) {
  return hmacHex(env.SYNC_KEY, `portal-access:${code}`);
}

/** As typed: any case, spaces or dashes, and O, I or L for 0, 1 and 1. */
export function normalizeAccessCode(code) {
  const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  const mapped = (code || "").toUpperCase().replace(/O/g, "0").replace(/[IL]/g, "1");
  return [...mapped].filter((ch) => alphabet.includes(ch)).join("").slice(0, 24);
}

/** Trammel signs every sync call: HMAC-SHA256, with the sync key, of
 * "<unix time>\n<METHOD>\n<path and query>\n<sha256 of the body>". A call more
 * than five minutes off is refused, so a copy can't be replayed later. */
export async function syncSignature(key, time, method, path, bodyBytes) {
  const digest = await sha256Hex(bodyBytes || new Uint8Array());
  return hmacHex(key, `${time}\n${method.toUpperCase()}\n${path}\n${digest}`);
}

export async function verifySync(request, env, bodyBytes) {
  const time = request.headers.get("X-Trammel-Time") || "";
  const given = request.headers.get("X-Trammel-Signature") || "";
  if (!env.SYNC_KEY || !/^\d+$/.test(time)) return false;
  if (Math.abs(Date.now() / 1000 - Number(time)) > 300) return false;
  const url = new URL(request.url);
  const wanted = await syncSignature(
    env.SYNC_KEY,
    time,
    request.method,
    url.pathname + url.search,
    bodyBytes,
  );
  return same(given, wanted);
}

/** What rate limits count an IP address as: itself, or for IPv6 its /64. */
export function ipKey(ip) {
  if (!ip) return "";
  if (!ip.includes(":")) return ip;
  if (ip.toLowerCase().startsWith("::ffff:") && ip.includes(".")) return ip.slice(7);
  const parts = expandIPv6(ip);
  return parts ? `${parts.slice(0, 4).join(":")}::/64` : ip;
}

function expandIPv6(ip) {
  const [head, tail] = ip.split("::");
  const a = head ? head.split(":") : [];
  const b = tail !== undefined && tail ? tail.split(":") : [];
  if (tail === undefined && a.length !== 8) return null;
  const fill = new Array(8 - a.length - b.length).fill("0");
  const all = tail === undefined ? a : [...a, ...fill, ...b];
  return all.map((p) => (p || "0").toLowerCase().replace(/^0+(?=.)/, ""));
}
