// What Trammel calls, always signed (crypto.verifySync): publish and remove
// records and files, read and acknowledge the inbox, end sessions, purge.

import { verifySync } from "./crypto.js";
import { SCHEMA_VERSION, ensureSchema } from "./db.js";

const BATCH = 50;
const KEY = /^[A-Za-z0-9][A-Za-z0-9/_.-]{0,250}$/;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

async function batches(db, statements) {
  for (let i = 0; i < statements.length; i += BATCH) {
    await db.batch(statements.slice(i, i + BATCH));
  }
}

async function purgeObjects(bucket, prefix = "") {
  let cursor;
  let removed = 0;
  do {
    const listed = await bucket.list({ prefix, cursor, limit: 1000 });
    const keys = listed.objects.map((o) => o.key);
    if (keys.length) {
      await bucket.delete(keys);
      removed += keys.length;
    }
    cursor = listed.truncated ? listed.cursor : undefined;
  } while (cursor);
  return removed;
}

export async function handleSync(request, env) {
  const body = new Uint8Array(await request.arrayBuffer());
  if (!(await verifySync(request, env, body))) return json({ error: "Not signed by Trammel." }, 403);
  await ensureSchema(env.DB);
  const url = new URL(request.url);
  const path = url.pathname.slice("/_sync/".length);
  const db = env.DB;
  const data = () => JSON.parse(new TextDecoder().decode(body) || "{}");

  if (path === "health" && request.method === "GET") {
    const records = await db.prepare("SELECT COUNT(*) AS n FROM records").first();
    const waiting = await db.prepare("SELECT COUNT(*) AS n FROM inbox").first();
    return json({
      ok: true,
      version: env.TRAMMEL_VERSION || "",
      schema: SCHEMA_VERSION,
      records: Number(records?.n || 0),
      inbox: Number(waiting?.n || 0),
    });
  }

  if (path === "records" && request.method === "POST") {
    const { upsert = [], delete: remove = [] } = data();
    const statements = [
      ...upsert.map((r) =>
        db
          .prepare(
            "INSERT OR REPLACE INTO records (type, id, customer, job, sort, data) VALUES (?, ?, ?, ?, ?, ?)",
          )
          .bind(r.type, r.id, r.customer ?? null, r.job ?? null, r.sort ?? "", JSON.stringify(r.data ?? {})),
      ),
      ...remove.map((r) => db.prepare("DELETE FROM records WHERE type = ? AND id = ?").bind(r.type, r.id)),
    ];
    await batches(db, statements);
    return json({ upserted: upsert.length, deleted: remove.length });
  }

  if (path.startsWith("objects/")) {
    const key = decodeURIComponent(path.slice("objects/".length));
    if (!KEY.test(key) || key.includes("..")) return json({ error: "Bad key." }, 400);
    if (request.method === "PUT") {
      await env.FILES.put(key, body, {
        httpMetadata: { contentType: request.headers.get("Content-Type") || "application/octet-stream" },
      });
      return json({ stored: key, size: body.length });
    }
    if (request.method === "DELETE") {
      await env.FILES.delete(key);
      return json({ deleted: key });
    }
    if (request.method === "GET") {
      const object = await env.FILES.get(key);
      if (!object) return json({ error: "Not found." }, 404);
      return new Response(object.body, {
        headers: { "Content-Type": object.httpMetadata?.contentType || "application/octet-stream" },
      });
    }
  }

  if (path === "inbox" && request.method === "GET") {
    const after = Number(url.searchParams.get("after") || 0);
    const limit = Math.min(Number(url.searchParams.get("limit") || 100), 500);
    const { results } = await db
      .prepare("SELECT seq, kind, at, ip, customer, data FROM inbox WHERE seq > ? ORDER BY seq LIMIT ?")
      .bind(after, limit)
      .all();
    return json({ events: (results || []).map((e) => ({ ...e, data: JSON.parse(e.data) })) });
  }

  if (path === "ack" && request.method === "POST") {
    const { upto = 0 } = data();
    await db.prepare("DELETE FROM inbox WHERE seq <= ?").bind(Number(upto)).run();
    return json({ acknowledged: Number(upto) });
  }

  if (path === "sessions/end" && request.method === "POST") {
    const { customer = null } = data();
    if (customer) await db.prepare("DELETE FROM sessions WHERE customer = ?").bind(customer).run();
    else await db.prepare("DELETE FROM sessions").run();
    return json({ ended: true });
  }

  if (path === "purge" && request.method === "POST") {
    await batches(
      db,
      ["records", "sessions", "codes", "attempts", "used_access_codes", "inbox"].map((t) =>
        db.prepare(`DELETE FROM ${t}`),
      ),
    );
    const removed = await purgeObjects(env.FILES);
    return json({ purged: true, objects: removed });
  }

  return json({ error: "No such call." }, 404);
}

/** The scheduled cleanup: expired sessions and codes, old attempts. */
export async function cleanup(env) {
  await ensureSchema(env.DB);
  const now = new Date().toISOString();
  const month = new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString();
  const db = env.DB;
  await db.batch([
    db.prepare("DELETE FROM sessions WHERE expires_at < ?").bind(now),
    db.prepare("DELETE FROM codes WHERE expires_at < ?").bind(month),
    db.prepare("DELETE FROM attempts WHERE at < ?").bind(month),
    db.prepare("DELETE FROM used_access_codes WHERE at < ?").bind(month),
  ]);
}
