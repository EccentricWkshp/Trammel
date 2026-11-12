// Sending email over SMTP from the Worker (sign-in codes), with Cloudflare's
// outbound TCP sockets. Only encrypted SMTP: SSL/TLS on connect (port 465) or
// STARTTLS (port 587); Cloudflare doesn't allow port 25.

import { connect } from "cloudflare:sockets";

const encoder = new TextEncoder();

export class MailError extends Error {}

function base64(text) {
  const bytes = encoder.encode(text);
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

function headerText(text) {
  // RFC 2047 for anything beyond plain ASCII.
  return /^[\x20-\x7e]*$/.test(text) ? text : `=?UTF-8?B?${base64(text)}?=`;
}

function wrap(text, width = 76) {
  const lines = [];
  for (let i = 0; i < text.length; i += width) lines.push(text.slice(i, i + width));
  return lines.join("\r\n");
}

/** The message: plain text, base64, with the headers a mail server expects. */
export function message({ fromAddress, fromName, to, subject, text, id }) {
  const domain = fromAddress.split("@")[1] || "localhost";
  const from = fromName ? `${headerText(fromName)} <${fromAddress}>` : fromAddress;
  return [
    `From: ${from}`,
    `To: ${to}`,
    `Subject: ${headerText(subject)}`,
    `Date: ${new Date().toUTCString().replace("GMT", "+0000")}`,
    `Message-ID: <${id}@${domain}>`,
    "MIME-Version: 1.0",
    "Content-Type: text/plain; charset=utf-8",
    "Content-Transfer-Encoding: base64",
    "",
    wrap(base64(text.replace(/\r?\n/g, "\r\n"))),
  ].join("\r\n");
}

class Line {
  constructor(socket) {
    this.socket = socket;
    this.reader = socket.readable.getReader();
    this.writer = socket.writable.getWriter();
    this.buffer = "";
    this.decoder = new TextDecoder();
  }

  async readLine() {
    for (;;) {
      const at = this.buffer.indexOf("\r\n");
      if (at >= 0) {
        const line = this.buffer.slice(0, at);
        this.buffer = this.buffer.slice(at + 2);
        return line;
      }
      const { value, done } = await this.reader.read();
      if (done) throw new MailError("The mail server closed the connection.");
      this.buffer += this.decoder.decode(value, { stream: true });
    }
  }

  /** A whole reply ("250-..." lines then "250 ..."), checked against `expect`. */
  async reply(expect) {
    const lines = [];
    for (;;) {
      const line = await this.readLine();
      lines.push(line);
      if (line.length < 4 || line[3] !== "-") break;
    }
    const code = Number(lines[lines.length - 1].slice(0, 3));
    if (!expect.includes(code)) {
      throw new MailError(`The mail server said: ${lines.join(" ").slice(0, 300)}`);
    }
    return lines;
  }

  async send(text) {
    await this.writer.write(encoder.encode(`${text}\r\n`));
  }

  release() {
    this.reader.releaseLock();
    this.writer.releaseLock();
  }
}

/**
 * Send one plain-text email. `smtp` is the shop's settings as Trammel pushed
 * them: {host, port, security ("ssl" | "starttls"), username, password,
 * from_address, from_name}.
 */
export async function sendMail(smtp, { to, subject, text }, { connectTo = connect } = {}) {
  if (!smtp || !smtp.host) throw new MailError("Email isn't set up.");
  if (!["ssl", "starttls"].includes(smtp.security)) {
    throw new MailError("Email must be encrypted (SSL/TLS on 465 or STARTTLS on 587).");
  }
  let socket = connectTo(
    { hostname: smtp.host, port: Number(smtp.port) },
    { secureTransport: smtp.security === "ssl" ? "on" : "starttls", allowHalfOpen: false },
  );
  let line = new Line(socket);
  const helo = "trammel-portal";
  try {
    await line.reply([220]);
    await line.send(`EHLO ${helo}`);
    await line.reply([250]);
    if (smtp.security === "starttls") {
      await line.send("STARTTLS");
      await line.reply([220]);
      line.release();
      socket = socket.startTls();
      line = new Line(socket);
      await line.send(`EHLO ${helo}`);
      await line.reply([250]);
    }
    if (smtp.username) {
      await line.send(`AUTH PLAIN ${base64(`\0${smtp.username}\0${smtp.password || ""}`)}`);
      await line.reply([235]);
    }
    await line.send(`MAIL FROM:<${smtp.from_address}>`);
    await line.reply([250]);
    await line.send(`RCPT TO:<${to}>`);
    await line.reply([250, 251]);
    await line.send("DATA");
    await line.reply([354]);
    const body = message({
      fromAddress: smtp.from_address,
      fromName: smtp.from_name || "",
      to,
      subject,
      text,
      id: crypto.randomUUID(),
    })
      .split("\r\n")
      .map((l) => (l.startsWith(".") ? `.${l}` : l))
      .join("\r\n");
    await line.send(`${body}\r\n.`);
    await line.reply([250]);
    await line.send("QUIT");
  } finally {
    try {
      await socket.close();
    } catch {
      // already closed
    }
  }
}
