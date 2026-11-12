// Trammel's edge portal (SPEC §12, release 0.9.x): the customer side of a shop,
// on Cloudflare, so the shop's own server never takes a connection from the
// internet. Trammel publishes what each customer may see and pulls back what
// they did; see sync.js for its calls and portal.js for the customer's pages.
//
// Bindings (set by Trammel when it deploys this): DB (D1), FILES (R2),
// SYNC_KEY and SMTP (secrets), TRAMMEL_VERSION.

import { ensureSchema, record } from "./db.js";
import { html, page, respond } from "./html.js";
import { handlePortal } from "./portal.js";
import { cleanup, handleSync } from "./sync.js";

function closed(site) {
  return respond(
    page({
      site,
      title: "Customer portal",
      body: html`<section class="card portal-narrow"><h1>The customer portal is closed</h1>
<p>Please contact us directly for now.</p></section>`,
    }),
    { status: 503 },
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/_sync/")) return handleSync(request, env);
    if (url.pathname === "/" || url.pathname === "/portal") {
      return Response.redirect(`${url.origin}/portal/`, 301);
    }
    if (!url.pathname.startsWith("/portal/")) {
      return new Response("Not found", { status: 404, headers: { "Content-Type": "text/plain" } });
    }
    await ensureSchema(env.DB);
    const config = await record(env.DB, "config", "site");
    const site = config ? config.data : null;
    if (!site || !site.portal_on) return closed(site);
    return handlePortal(request, env, site);
  },

  async scheduled(_event, env) {
    await cleanup(env);
  },
};
