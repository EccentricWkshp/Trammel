// Stand-ins for Cloudflare's bindings: D1 on Node's own SQLite, R2 in memory, a
// mail server that follows the SMTP script, and signed sync calls.
import { DatabaseSync } from "node:sqlite";

import { syncSignature } from "../crypto.js";
import { _resetSchemaFlag } from "../db.js";

class Statement {
  constructor(db, sql, args = []) {
    this.db = db;
    this.sql = sql;
    this.args = args;
  }

  bind(...args) {
    return new Statement(this.db, this.sql, args);
  }

  async first() {
    return this.db.prepare(this.sql).get(...this.args) ?? null;
  }

  async all() {
    return { results: this.db.prepare(this.sql).all(...this.args) };
  }

  async run() {
    const result = this.db.prepare(this.sql).run(...this.args);
    return { meta: { changes: Number(result.changes) } };
  }
}

export function fakeD1() {
  _resetSchemaFlag();
  const db = new DatabaseSync(":memory:");
  return {
    prepare: (sql) => new Statement(db, sql),
    async batch(statements) {
      db.exec("BEGIN");
      try {
        const out = [];
        for (const s of statements) out.push(await s.run());
        db.exec("COMMIT");
        return out;
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    },
    raw: db,
  };
}

function bytes(body) {
  if (body instanceof Uint8Array) return body;
  if (body instanceof ArrayBuffer) return new Uint8Array(body);
  return new TextEncoder().encode(String(body));
}

export function fakeR2() {
  const store = new Map();
  return {
    store,
    async put(key, body, options = {}) {
      store.set(key, { bytes: bytes(body), httpMetadata: options.httpMetadata || {}, uploaded: new Date() });
    },
    async get(key) {
      const found = store.get(key);
      if (!found) return null;
      return {
        body: new Blob([found.bytes]).stream(),
        httpMetadata: found.httpMetadata,
        async text() {
          return new TextDecoder().decode(found.bytes);
        },
      };
    },
    async delete(keys) {
      for (const key of Array.isArray(keys) ? keys : [keys]) store.delete(key);
    },
    async list({ prefix = "" } = {}) {
      return {
        objects: [...store.entries()]
          .filter(([k]) => k.startsWith(prefix))
          .map(([key, o]) => ({ key, uploaded: o.uploaded })),
        truncated: false,
      };
    },
  };
}

export function env(extra = {}) {
  return {
    DB: fakeD1(),
    FILES: fakeR2(),
    SYNC_KEY: "test-sync-key",
    SMTP: JSON.stringify({
      host: "mail.example.com",
      port: 465,
      security: "ssl",
      username: "shop",
      password: "secret",
      from_address: "shop@example.com",
      from_name: "Test Shop",
    }),
    TRAMMEL_VERSION: "0.9.0",
    ...extra,
  };
}

/** A signed call from Trammel. */
export async function syncRequest(environment, method, path, data = undefined, headers = {}) {
  const body =
    data === undefined
      ? new Uint8Array()
      : data instanceof Uint8Array
        ? data
        : new TextEncoder().encode(JSON.stringify(data));
  const time = String(Math.floor(Date.now() / 1000));
  const signature = await syncSignature(environment.SYNC_KEY, time, method, path, body);
  return new Request(`https://portal.example.com${path}`, {
    method,
    body: method === "GET" ? undefined : body,
    headers: { "X-Trammel-Time": time, "X-Trammel-Signature": signature, ...headers },
  });
}

/** A browser's request, from the portal's own pages. */
export function browser(method, path, { form = null, cookie = "", ip = "203.0.113.9" } = {}) {
  const headers = { "CF-Connecting-IP": ip, Origin: "https://portal.example.com" };
  if (cookie) headers.Cookie = cookie;
  const init = { method, headers };
  if (form) {
    init.body = new URLSearchParams(form).toString();
    headers["Content-Type"] = "application/x-www-form-urlencoded";
  }
  return new Request(`https://portal.example.com${path}`, init);
}

export function cookieFrom(response) {
  const header = response.headers.get("Set-Cookie") || "";
  return header.split(";")[0];
}

/** A mail server following the SMTP script; what it was sent ends up in
 * `server.messages`. Use as globalThis.__connect. */
export function fakeMailServer({ failAuth = false } = {}) {
  const server = { messages: [], commands: [], tls: [] };
  server.connect = (address, options) => {
    server.tls.push(options.secureTransport);
    return socket(server, failAuth);
  };
  return server;
}

function socket(server, failAuth) {
  const encoder = new TextEncoder();
  let controller;
  const readable = new ReadableStream({
    start(c) {
      controller = c;
      c.enqueue(encoder.encode("220 mail.example.com ESMTP\r\n"));
    },
  });
  let data = null;
  let pending = "";
  const reply = (text) => controller.enqueue(encoder.encode(`${text}\r\n`));
  const writable = new WritableStream({
    write(chunk) {
      pending += new TextDecoder().decode(chunk);
      let at;
      while ((at = pending.indexOf("\r\n")) >= 0) {
        const line = pending.slice(0, at);
        pending = pending.slice(at + 2);
        if (data !== null) {
          if (line === ".") {
            server.messages.push(data.join("\r\n"));
            data = null;
            reply("250 queued");
          } else data.push(line);
          continue;
        }
        server.commands.push(line.split(" ")[0]);
        if (line.startsWith("EHLO")) reply("250-mail.example.com\r\n250 AUTH PLAIN");
        else if (line === "STARTTLS") reply("220 go ahead");
        else if (line.startsWith("AUTH")) reply(failAuth ? "535 no" : "235 ok");
        else if (line.startsWith("MAIL") || line.startsWith("RCPT")) reply("250 ok");
        else if (line === "DATA") {
          data = [];
          reply("354 go");
        } else if (line === "QUIT") reply("221 bye");
      }
    },
  });
  const sock = {
    readable,
    writable,
    startTls() {
      server.tls.push("upgraded");
      return sock;
    },
    async close() {},
  };
  return sock;
}

export function decodeMessage(raw) {
  const [, body] = raw.split("\r\n\r\n");
  return Buffer.from(body.replace(/\r\n/g, ""), "base64").toString("utf8");
}
