// HTML for the edge portal's pages: escaping, the page frame, and responses
// with the headers every page carries.

export function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

/** A template literal whose values are escaped unless already safe(). */
export function html(strings, ...values) {
  let out = "";
  strings.forEach((part, i) => {
    out += part;
    if (i < values.length) out += render(values[i]);
  });
  return new Safe(out);
}

class Safe {
  constructor(text) {
    this.text = text;
  }

  toString() {
    return this.text;
  }
}

export function safe(text) {
  return new Safe(text);
}

function render(value) {
  if (value instanceof Safe) return value.text;
  if (Array.isArray(value)) return value.map(render).join("");
  if (value === null || value === undefined || value === false) return "";
  return esc(value);
}

/** Every page's headers: no script but our own, nothing from elsewhere. */
const SECURITY = {
  "Content-Security-Policy":
    "default-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; " +
    "frame-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "same-origin",
  "Strict-Transport-Security": "max-age=31536000",
  "Cache-Control": "no-store",
};

/** A shop document (a quote, an invoice): its own styles, shown framed in the
 * portal's own page, never running script. */
export const DOCUMENT_HEADERS = {
  "Content-Security-Policy":
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; " +
    "frame-ancestors 'self'; sandbox",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "same-origin",
  "Cache-Control": "no-store",
};

export function respond(body, { status = 200, headers = {} } = {}) {
  return new Response(String(body), {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", ...SECURITY, ...headers },
  });
}

export function redirect(location, headers = {}) {
  return new Response(null, { status: 303, headers: { Location: location, ...SECURITY, ...headers } });
}

const NOTICES = {
  "code-sent":
    "If that address is on file with us, we've sent it a sign-in code. It's good for 10 minutes.",
  "signed-out": "You're signed out.",
  "answer-received": "Thank you. We've received your answer and will confirm it here shortly.",
  "files-sent": "Thank you. We've received your files and will look at them shortly.",
};
/** Problems, shown as errors. Only these texts are ever shown from a link. */
const PROBLEMS = {
  "code-wrong": "That code didn't work. Check it, or ask for a new one.",
  "access-wrong": "That code didn't work. Check it, or contact us for a new one.",
  "mail-failed": "We couldn't send the email just now. Please try again in a few minutes.",
  "answer-closed": "This can no longer be answered here.",
  "answer-name": "Enter your full name to approve.",
  "answer-terms-changed": "The terms have changed since this page was opened. Please read them again.",
  "answer-accept": "Tick the box to accept the terms.",
  "answer-sign": "Sign in the box to approve.",
  "send-off": "Sending files isn't available right now.",
  "send-none": "Choose at least one file to send.",
  "send-too-many": "That's more files than can be sent at once.",
  "send-too-big": "A file is larger than we can take.",
};

/** The page frame. `site` is the shop's published settings; `visitor` the
 * signed-in customer, if any. */
export function page({ site, title, visitor = null, notice = "", body }) {
  const business = site?.business || {};
  const text = NOTICES[notice] || PROBLEMS[notice];
  const kind = PROBLEMS[notice] ? "error" : "info";
  return html`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>${title} · ${business.name || "Customer portal"}</title>
  ${site?.css ? html`<link rel="stylesheet" href="/portal/static/${site.css}">` : ""}
</head>
<body class="portal">
<a class="skip-link" href="#main">Skip to content</a>
<header class="topbar portal-bar">
  <a class="brand" href="/portal/">${business.logo ? html`<img src="${business.logo}" alt="" class="portal-logo">` : ""}<span>${business.name || "Customer portal"}</span></a>
  ${
    visitor
      ? html`<div class="portal-who">
    <span>${visitor.name}${visitor.name !== visitor.customerName ? html` · ${visitor.customerName}` : ""}</span>
    <form method="post" action="/portal/sign-out/"><button type="submit" class="linklike">Sign out</button></form>
  </div>`
      : ""
  }
</header>
<main id="main" class="page portal-page">
  ${text ? html`<div class="messages" role="${kind === "error" ? "alert" : "status"}"><p class="message ${kind}">${text}</p></div>` : ""}
  ${body}
</main>
<footer class="footer">${business.name || ""}${business.phone ? html` · ${business.phone}` : ""}${business.email ? html` · ${business.email}` : ""}</footer>
</body>
</html>`;
}
