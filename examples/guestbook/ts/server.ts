// The guestbook ported to TypeScript (node 23.6+ runs it as is, no build, no packages): it must behave exactly
// like ../php/index.php.
//
//   node server.ts 8080        (GUESTBOOK_DATA: the data folder)

import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

const NAME_MAX = 40;
const MESSAGE_MAX = 200;
const DATA: string = process.env.GUESTBOOK_DATA || path.join(import.meta.dirname, "data");
const FILE: string = path.join(DATA, "entries.json");

interface Entry {
  id: string;
  name: string;
  message: string;
  at: string;
}

interface Answer {
  status: number;
  body: string;
  type?: string;
  location?: string;
}

// htmlspecialchars(ENT_QUOTES): a single quote is &#039;
const e = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
const chars = (s: string): number => [...s].length; // code points, like preg_match_all('/./su')
const phpTrim = (s: string): string => s.replace(/^[ \t\n\r\0\x0B]+|[ \t\n\r\0\x0B]+$/g, ""); // PHP keeps NBSP
const nl2br = (s: string): string => s.replace(/(\r\n|\n\r|\n|\r)/g, "<br />$1");
const now = (): string => new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"); // gmdate('c')
const minute = (at: string): string => new Date(at).toISOString().slice(0, 16).replace("T", " ");

function loadEntries(): Entry[] {
  return fs.existsSync(FILE) ? JSON.parse(fs.readFileSync(FILE, "utf8")) as Entry[] : [];
}

function saveEntries(entries: Entry[]): void {
  fs.mkdirSync(DATA, { recursive: true });
  fs.writeFileSync(FILE, JSON.stringify(entries, null, 4));
}

function cookies(req: http.IncomingMessage): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (req.headers.cookie || "").split(";")) {
    const at = part.indexOf("=");
    if (at > 0) out[part.slice(0, at).trim()] = decodeURIComponent(part.slice(at + 1).trim());
  }
  return out;
}

class Visit {
  method: string;
  path: string;
  cookies: Record<string, string>;
  form: URLSearchParams;
  headers: Record<string, string> = {};
  made: string | null = null;

  constructor(req: http.IncomingMessage, body: string) {
    const url = new URL(req.url || "/", "http://localhost");
    this.method = req.method || "GET";
    this.path = url.pathname;
    this.cookies = cookies(req);
    this.form = new URLSearchParams(req.headers["content-type"] === "application/x-www-form-urlencoded" ? body : "");
  }

  field(name: string): string { // PHP's $_POST: the last value of a repeated field
    const all = this.form.getAll(name);
    return all.length ? all[all.length - 1] : "";
  }

  token(): string {
    if (this.cookies.csrf) return this.cookies.csrf;
    if (this.made === null) {
      this.made = crypto.randomBytes(16).toString("hex");
      this.headers["Set-Cookie"] = `csrf=${this.made}; path=/; HttpOnly; SameSite=Lax`;
    }
    return this.made;
  }

  tokenOk(): boolean {
    const sent = this.field("csrf");
    return Boolean(this.cookies.csrf) && sent !== "" && sent === this.cookies.csrf;
  }
}

function page(status: number, title: string, body: string): Answer {
  return {
    status, type: "text/html; charset=utf-8",
    body: "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n" +
      `  <title>${e(title)} · Guestbook</title>\n</head>\n<body>\n` +
      `  <h1><a href="/">Guestbook</a></h1>\n${body}</body>\n</html>\n`,
  };
}

const redirect = (to: string): Answer => ({ status: 303, location: to, body: "" });
const notFound = (): Answer => page(404, "Not found", "  <p>Page not found.</p>\n");
const expired = (): Answer => page(403, "Expired", "  <p>Your session expired. Reload the page and try again.</p>\n");

function index(v: Visit, entries: Entry[], errors: string[] = [], name = "", message = "", status = 200): Answer {
  let body = "  <form method=\"post\" action=\"/sign\">\n";
  body += `    <input type="hidden" name="csrf" value="${e(v.token())}">\n`;
  if (errors.length) {
    body += "    <ul class=\"errors\">\n";
    for (const error of errors) body += `      <li>${e(error)}</li>\n`;
    body += "    </ul>\n";
  }
  body += `    <label>Name <input name="name" maxlength="${NAME_MAX}" value="${e(name)}"></label>\n`;
  body += `    <label>Message <textarea name="message">${e(message)}</textarea></label>\n`;
  body += "    <button>Sign</button>\n  </form>\n";
  body += `  <h2>${entries.length}${entries.length === 1 ? " entry" : " entries"}</h2>\n`;
  if (!entries.length) {
    body += "  <p class=\"empty\">No entries yet. Be the first!</p>\n";
  } else {
    body += "  <ul class=\"entries\">\n";
    for (const entry of [...entries].reverse()) {
      body += `    <li><a href="/entries/${e(entry.id)}">${e(entry.name)}</a>: ${e(entry.message)}</li>\n`;
    }
    body += "  </ul>\n";
  }
  return page(status, "Sign the guestbook", body);
}

function show(v: Visit, entry: Entry): Answer {
  let body = `  <article>\n    <h2>${e(entry.name)}</h2>\n`;
  body += `    <p>${nl2br(e(entry.message))}</p>\n`;
  body += `    <p class="meta">Signed ${minute(entry.at)} UTC</p>\n`;
  body += `    <form method="post" action="/entries/${e(entry.id)}/delete">\n`;
  body += `      <input type="hidden" name="csrf" value="${e(v.token())}">\n`;
  body += "      <button>Delete</button>\n    </form>\n  </article>\n";
  return page(200, entry.name, body);
}

function sign(v: Visit, entries: Entry[]): Answer {
  if (!v.tokenOk()) return expired();
  const name = phpTrim(v.field("name"));
  const message = phpTrim(v.field("message"));
  const errors: string[] = [];
  if (name === "") errors.push("Please write your name.");
  else if (chars(name) > NAME_MAX) errors.push(`Your name can be at most ${NAME_MAX} characters.`);
  if (message === "") errors.push("Please write a message.");
  else if (chars(message) > MESSAGE_MAX) errors.push(`Your message can be at most ${MESSAGE_MAX} characters.`);
  if (errors.length) return index(v, entries, errors, name, message, 422);
  const entry: Entry = { id: crypto.randomBytes(4).toString("hex"), name, message, at: now() };
  saveEntries([...entries, entry]);
  return redirect(`/entries/${entry.id}`);
}

function route(v: Visit): Answer {
  const entries = loadEntries();
  if (v.method === "GET" && v.path === "/health") return { status: 200, type: "text/plain; charset=utf-8", body: "ok" };
  if (v.method === "GET" && v.path === "/") return index(v, entries);
  if (v.method === "POST" && v.path === "/sign") return sign(v, entries);
  const shown = v.path.match(/^\/entries\/([0-9a-f]+)$/);
  if (shown && v.method === "GET") {
    const entry = entries.find((x) => x.id === shown[1]);
    return entry ? show(v, entry) : notFound();
  }
  const deleted = v.path.match(/^\/entries\/([0-9a-f]+)\/delete$/);
  if (deleted && v.method === "POST") {
    if (!v.tokenOk()) return expired();
    const left = entries.filter((x) => x.id !== deleted[1]);
    if (left.length === entries.length) return notFound();
    saveEntries(left);
    return redirect("/");
  }
  return notFound();
}

const server = http.createServer((req, res) => {
  const chunks: Buffer[] = [];
  req.on("data", (c: Buffer) => chunks.push(c));
  req.on("end", () => {
    const v = new Visit(req, Buffer.concat(chunks).toString("utf8"));
    const out = route(v);
    const headers: Record<string, string> = { ...v.headers };
    if (out.type) headers["Content-Type"] = out.type;
    if (out.location) headers.Location = out.location;
    res.writeHead(out.status, headers);
    res.end(out.body);
  });
});

server.listen(Number(process.argv[2] || 8080), "127.0.0.1");
