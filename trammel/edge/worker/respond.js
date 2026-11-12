// Approving or declining a quote or change order on the edge portal (FR-PRT-3,
// FR-QUO-11, FR-AGR-7): the customer's choices, the terms, their name and
// signature. Recorded here and passed to Trammel, which applies it through the
// same services as its own portal and publishes the result.

import { inbox } from "./db.js";
import { html } from "./html.js";

const SIGNATURE_PREFIX = "data:image/png;base64,";
const SIGNATURE_MAX = 400_000;

/** The ids picked on a page or a form: alternatives (pick-N) and options (pick). */
export function picksFrom(params) {
  const out = [];
  for (const [key, value] of params.entries()) {
    if (key === "pick" || key.startsWith("pick-")) out.push(String(value));
  }
  return out;
}

/** The choices as picked: each group's pick (else its default), the options
 * ticked; and the key Trammel's totals table uses. */
export function picked(respond, picks) {
  const wanted = new Set(picks);
  const groups = (respond.choices?.groups || []).map((g) => {
    const chosen = g.lines.find((l) => wanted.has(l.id)) || g.lines.find((l) => l.id === g.default);
    return { ...g, chosen: chosen ? chosen.id : g.default };
  });
  const options = (respond.choices?.options || []).map((o) => ({ ...o, picked: wanted.has(o.id) }));
  const ids = [...groups.map((g) => g.chosen), ...options.filter((o) => o.picked).map((o) => o.id)];
  return { groups, options, ids, key: [...ids].sort().join(",") };
}

function choicesForm(q, choice) {
  const figures = q.respond.totals?.[choice.key];
  return html`<section class="card choices" id="choices" aria-labelledby="choices-heading">
  <h2 id="choices-heading">Your choices</h2>
  <form method="get" action="#choices" class="stack">
    ${choice.groups.map(
      (g, i) => html`<fieldset><legend>${g.name}: pick one</legend>${g.lines.map(
        (l) => html`<label class="choice"><input type="radio" name="pick-${i + 1}" value="${l.id}"${l.id === g.chosen ? html` checked` : ""}>
          <span>${l.description}</span> <span class="muted">${l.price}</span></label>`,
      )}</fieldset>`,
    )}
    ${
      choice.options.length
        ? html`<fieldset><legend>Options: add any you'd like</legend>${choice.options.map(
            (o) => html`<label class="choice"><input type="checkbox" name="pick" value="${o.id}"${o.picked ? html` checked` : ""}>
          <span>${o.description}</span> <span class="muted">+${o.price}</span></label>`,
          )}</fieldset>`
        : ""
    }
    <p class="choices-total">${
      figures
        ? html`Total with these choices: <strong>${figures.total}</strong>${figures.deposit ? html`, with a deposit of ${figures.deposit} on approval` : ""}.`
        : html`We'll confirm the total with your choices when we receive your approval.`
    }</p>
    <div class="actions"><button type="submit">Update the total</button></div>
  </form>
</section>`;
}

/** The approve and decline forms, or what happened to an answer already given. */
export function respondSection(site, visitor, found, picks) {
  const q = found.data;
  if (q.pending) {
    return html`<p class="card message info" role="status">Thank you. We've received your ${q.pending.answer === "approve" ? "approval" : "answer"} and will confirm it here shortly.</p>`;
  }
  const problem = q.problem ? html`<p class="card message error" role="alert">${q.problem}</p>` : "";
  if (!q.open || !q.respond) return problem;
  const r = q.respond;
  const choice = picked(r, picks);
  const figures = r.totals?.[choice.key];
  const total = figures ? figures.total : q.total;
  return html`${problem}
${r.choices && (r.choices.groups.length || r.choices.options.length) ? choicesForm(q, choice) : ""}
${r.signing && site.signature_js ? html`<script src="/portal/static/${site.signature_js}" defer></script>` : ""}
<section class="card" aria-labelledby="approve-heading">
  <h2 id="approve-heading">Approve</h2>
  <form method="post" action="/portal/quotes/${found.id}/respond/" class="stack">
    <input type="hidden" name="answer" value="approve">
    ${choice.ids.map((id) => html`<input type="hidden" name="pick" value="${id}">`)}
    ${
      r.agreement
        ? html`<input type="hidden" name="terms" value="${r.agreement.hash}">
    <h3>${r.agreement.title}</h3>
    <div class="agreement-text prewrap" tabindex="0" aria-label="${r.agreement.title}">${r.agreement.text}</div>
    <div class="field field-check"><label><input type="checkbox" name="accept" value="1" required> I have read and accept the ${r.agreement.title.toLowerCase()}.</label></div>`
        : ""
    }
    <div class="field"><label for="approve-name">Your full name</label>
      <input type="text" id="approve-name" name="name" value="${visitor.name}" maxlength="150" autocomplete="name" required></div>
    ${
      r.signing
        ? html`<div class="field signature-field">
  <span class="signature-label" id="signature-label">Your signature</span>
  <div class="signature-box" data-signature="signature">
    <canvas class="signature-pad" role="img" aria-labelledby="signature-label"></canvas>
    <button type="button" class="linklike" data-signature-clear>Clear</button>
  </div>
  <input type="hidden" id="signature" name="signature">
  <p class="muted small">Sign with your finger, a stylus or the mouse.</p>
  <noscript><p class="message warning">Signing needs JavaScript turned on in this browser.</p></noscript>
</div>`
        : ""
    }
    <div class="field"><label for="approve-comment">Comment (optional)</label>
      <textarea id="approve-comment" name="comment" rows="2" maxlength="2000"></textarea></div>
    <p class="muted small">Approving records your name${r.signing ? " and signature" : ""}, the time, and your IP address${r.agreement ? ", as your acceptance of the terms above" : ""}.</p>
    <div class="actions"><button type="submit" class="primary">Approve ${q.noun} for ${total}</button></div>
  </form>
</section>
<section class="card" aria-labelledby="decline-heading">
  <h2 id="decline-heading">Decline</h2>
  <form method="post" action="/portal/quotes/${found.id}/respond/" class="stack">
    <input type="hidden" name="answer" value="decline">
    <div class="field"><label for="decline-comment">Tell us why, or what would work better (optional)</label>
      <textarea id="decline-comment" name="comment" rows="2" maxlength="2000"></textarea></div>
    <div class="actions"><button type="submit">Decline</button></div>
  </form>
</section>`;
}

/** Take an answer: checked as far as can be here, recorded, passed on. Returns
 * a problem (a notice's key, html.js), or "" when it's been received. */
export async function takeAnswer(env, visitor, found, form, ip) {
  const q = found.data;
  if (!q.open || !q.respond || q.pending) return "answer-closed";
  const answer = String(form.get("answer") || "");
  const comment = String(form.get("comment") || "").trim().slice(0, 2000);
  const data = {
    revision: found.id,
    answer,
    comment,
    email: visitor.email || "",
    how: visitor.how,
    given_by: visitor.given_by || "",
  };
  if (answer === "approve") {
    const name = String(form.get("name") || "").trim().slice(0, 150);
    if (!name) return "answer-name";
    const r = q.respond;
    if (r.agreement) {
      if (String(form.get("terms") || "") !== r.agreement.hash) return "answer-terms-changed";
      if (form.get("accept") !== "1") return "answer-accept";
    }
    const signature = String(form.get("signature") || "");
    if (r.signing && (!signature.startsWith(SIGNATURE_PREFIX) || signature.length > SIGNATURE_MAX)) {
      return "answer-sign";
    }
    Object.assign(data, {
      name,
      terms: r.agreement ? r.agreement.hash : "",
      picks: picksFrom(form),
      signature: r.signing ? signature : "",
    });
  } else if (answer !== "decline") {
    return "answer-closed";
  }
  const pending = { ...q, pending: { answer, at: new Date().toISOString() }, problem: "" };
  await env.DB.batch([
    env.DB.prepare("UPDATE records SET data = ? WHERE type = 'quote' AND id = ?").bind(
      JSON.stringify(pending),
      found.id,
    ),
  ]);
  await inbox(env.DB, "quote_response", data, { ip, customer: visitor.customerId });
  return "";
}
