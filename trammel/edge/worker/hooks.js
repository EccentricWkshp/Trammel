// Online stores reaching Trammel through the edge portal, at the same paths as
// Trammel's own (FR-IMP): Shopify's order webhooks and eBay's account deletion
// notices are kept in the inbox, unchecked, for Trammel to check and apply;
// Etsy's and eBay's sign-in callbacks send the owner's browser on to Trammel.

import { sha256Hex } from "./crypto.js";
import { inbox } from "./db.js";

const MAX_BODY = 1_000_000;

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
    const text = await body(request);
    if (text === null) return plain("Too large", 413);
    await inbox(env.DB, "shopify_webhook", {
      body: text,
      hmac: request.headers.get("X-Shopify-Hmac-Sha256") || "",
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
