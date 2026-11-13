// Online stores reaching Trammel through the edge portal, at the same paths as
// Trammel's own (FR-IMP): Shopify's order webhooks and eBay's account deletion
// notices are kept in the inbox for Trammel to check again and apply; one with
// no signature, or a Shopify one whose signature is wrong, is turned away here
// (SEC-08, 0.10.6). Etsy's and eBay's sign-in callbacks send the owner's
// browser on to Trammel.

import { noteAttempt, recentFrom } from "./auth.js";
import { same, sha256Hex, shopifySignature } from "./crypto.js";
import { inbox } from "./db.js";

const MAX_BODY = 1_000_000;
/** Past this many store notices waiting, more are turned away (the stores
 * retry). Counted apart from what customers do, which always has room
 * (SEC-08, 0.10.6). */
export const MAX_WAITING = 1000;
/** Store notices one address may send in an hour. */
export const PER_HOUR = 600;
const HOOK_KINDS = "('shopify_webhook', 'ebay_deletion')";

async function full(env) {
  const row = await env.DB.prepare(`SELECT COUNT(*) AS n FROM inbox WHERE kind IN ${HOOK_KINDS}`).first();
  return Number(row?.n || 0) >= MAX_WAITING;
}

/** Turned away before the body is read: no signature, too many from this
 * address, or too many waiting. A Response, or null to go on. */
async function refuse(request, env, header) {
  if (!request.headers.get(header)) return plain("Unsigned", 401);
  const ip = request.headers.get("CF-Connecting-IP") || "";
  if ((await recentFrom(env, "hook", ip)) >= PER_HOUR) return plain("Too many", 429);
  await noteAttempt(env, "hook", ip);
  if (await full(env)) return plain("Busy", 503);
  return null;
}

/** Shopify's secret, when Trammel has given it; "none" is a deploy without one. */
function shopifySecret(env) {
  const secret = env.SHOPIFY_WEBHOOK_SECRET || "";
  return secret && secret !== "none" ? secret : "";
}

function plain(text, status) {
  return new Response(text, { status, headers: { "Content-Type": "text/plain" } });
}

async function body(request) {
  const length = Number(request.headers.get("Content-Length") || 0);
  if (length > MAX_BODY) return null;
  const text = await request.text();
  return text.length > MAX_BODY ? null : text;
}

export async function handleHook(request, env, site) {
  const url = new URL(request.url);
  const hooks = site?.hooks || {};
  const path = url.pathname;

  if (path === "/imports/shopify/webhook/" && request.method === "POST" && hooks.shopify) {
    const turned = await refuse(request, env, "X-Shopify-Hmac-Sha256");
    if (turned) return turned;
    const text = await body(request);
    if (text === null) return plain("Too large", 413);
    const secret = shopifySecret(env);
    const signature = request.headers.get("X-Shopify-Hmac-Sha256") || "";
    if (secret && !same(await shopifySignature(secret, text), signature)) {
      // Checked here as well as in Trammel, so a forged one never waits.
      return plain("Bad signature", 401);
    }
    await inbox(env.DB, "shopify_webhook", {
      body: text,
      hmac: signature,
      shop: request.headers.get("X-Shopify-Shop-Domain") || "",
      topic: request.headers.get("X-Shopify-Topic") || "",
    });
    return plain("OK", 200);
  }

  if ((path === "/imports/etsy/callback/" && hooks.etsy) || (path === "/imports/ebay/callback/" && hooks.ebay)) {
    // The shop owner, signing a store in: their browser can reach Trammel.
    if (!site.local_url) return plain("Not found", 404);
    return Response.redirect(`${site.local_url}${path}${url.search}`, 302);
  }

  if (path === "/imports/ebay/deletion/" && hooks.ebay_deletion && env.EBAY_TOKEN) {
    if (request.method === "GET") {
      // eBay checks the address: the challenge, the token and the address, hashed.
      const challenge = url.searchParams.get("challenge_code") || "";
      const endpoint = `${url.origin}${path}`;
      const response = await sha256Hex(challenge + env.EBAY_TOKEN + endpoint);
      return new Response(JSON.stringify({ challengeResponse: response }), {
        headers: { "Content-Type": "application/json" },
      });
    }
    if (request.method === "POST") {
      // eBay's signature is checked by Trammel, with eBay's public key.
      const turned = await refuse(request, env, "X-EBAY-SIGNATURE");
      if (turned) return turned;
      const text = await body(request);
      if (text === null) return plain("Too large", 413);
      await inbox(env.DB, "ebay_deletion", {
        body: text,
        signature: request.headers.get("X-EBAY-SIGNATURE") || "",
      });
      return new Response(null, { status: 204 });
    }
  }
  return plain("Not found", 404);
}
