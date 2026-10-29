// The guestbook ported to JavaScript (node, no packages): it must behave exactly like ../php/index.php.
//
//   node server.js 8080        (GUESTBOOK_DATA: the data folder)

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const NAME_MAX = 40;
const MESSAGE_MAX = 200;
const DATA = process.env.GUESTBOOK_DATA || path.join(__dirname, "data");
const FILE = path.join(DATA, "entries.json");

// htmlspecialchars(ENT_QUOTES): a single quote is &#039;
const e = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
const chars = (s) => [...s].length; // code points, like preg_match_all('/./su')
const phpTrim = (s) => s.replace(/^[ \t\n\r\0\x0B]+|[ \t\n\r\0\x0B]+$/g, ""); // not String.trim: PHP keeps NBSP
const nl2br = (s) => s.replace(/(\r\n|\n\r|\n|\r)/g, "<br />$1");
const now = () => new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"); // gmdate('c')
const minute = (at) => new Date(at).toISOString().slice(0, 16).replace("T", " ");

function loadEntries() {
  return fs.existsSync(FILE) ? JSON.parse(fs.readFileSync(FILE, "utf8")) : [];
}

function saveEntries(entries) {
  fs.mkdirSync(DATA, { recursive: true });
  fs.writeFileSync(FILE, JSON.stringify(entries, null, 4));
}

function cookies(req) {
  const out = {};
  for (const part of (req.headers.cookie || "").split(";")) {
    const at = part.indexOf("=");
    if (at > 0) out[part.slice(0, at).trim()] = decodeURIComponent(part.slice(at + 1).trim());
  }
  return out;
}

class Request {
  constructor(req, body) {
    const url = new URL(req.url, "http://localhost");
    this.method = req.method;
    this.path = url.pathname;
    this.cookies = cookies(req);
    this.form = new URLSearchParams(req.headers["content-type"] === "application/x-www-form-urlencoded" ? body : "");
    this.headers = {};
    this.made = null;
  }

  field(name) { // PHP's $_POST: the last value of a repeated field
    const all = this.form.getAll(name);
    return all.length ? all[all.length - 1] : "";
  }

  token() {
    if (this.cookies.csrf) return this.cookies.csrf;
    if (this.made === null) {
      this.made = crypto.randomBytes(16).toString("hex");
      this.headers["Set-Cookie"] = `csrf=${this.made}; path=/; HttpOnly; SameSite=Lax`;
    }
    return this.made;
  }

  tokenOk() {
    const sent = this.field("csrf");
    return Boolean(this.cookies.csrf) && sent !== "" && sent === this.cookies.csrf;
  }
}

function page(status, title, body) {
  return {
    status, type: "text/html; charset=utf-8",
    body: "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n" +
      `  <title>${e(title)} · Guestbook</title>\n</head>\n<body>\n` +
      `  <h1><a href="/">Guestbook</a></h1>\n${body}</body>\n</html>\n`,
  };
}

const redirect = (to) => ({ status: 303, location: to, body: "" });
const notFound = () => page(404, "Not found", "  <p>Page not found.</p>\n");
const expired = () => page(403, "Expired", "  <p>Your session expired. Reload the page and try again.</p>\n");

function index(rq, entries, errors = [], name = "", message = "", status = 200) {
  let body = "  <form method=\"post\" action=\"/sign\">\n";
  body += `    <input type="hidden" name="csrf" value="${e(rq.token())}">\n`;
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

function show(rq, entry) {
  let body = `  <article>\n    <h2>${e(entry.name)}</h2>\n`;
  body += `    <p>${nl2br(e(entry.message))}</p>\n`;
  body += `    <p class="meta">Signed ${minute(entry.at)} UTC</p>\n`;
  body += `    <form method="post" action="/entries/${e(entry.id)}/delete">\n`;
  body += `      <input type="hidden" name="csrf" value="${e(rq.token())}">\n`;
  body += "      <button>Delete</button>\n    </form>\n  </article>\n";
  return page(200, entry.name, body);
}

function sign(rq, entries) {
  if (!rq.tokenOk()) return expired();
  const name = phpTrim(rq.field("name"));
  const message = phpTrim(rq.field("message"));
  const errors = [];
  if (name === "") errors.push("Please write your name.");
  else if (chars(name) > NAME_MAX) errors.push(`Your name can be at most ${NAME_MAX} characters.`);
  if (message === "") errors.push("Please write a message.");
  else if (chars(message) > MESSAGE_MAX) errors.push(`Your message can be at most ${MESSAGE_MAX} characters.`);
  if (errors.length) return index(rq, entries, errors, name, message, 422);
  const entry = { id: crypto.randomBytes(4).toString("hex"), name, message, at: now() };
  saveEntries([...entries, entry]);
  return redirect(`/entries/${entry.id}`);
}

function route(rq) {
  const entries = loadEntries();
  if (rq.method === "GET" && rq.path === "/health") return { status: 200, type: "text/plain; charset=utf-8", body: "ok" };
  if (rq.method === "GET" && rq.path === "/") return index(rq, entries);
  if (rq.method === "POST" && rq.path === "/sign") return sign(rq, entries);
  let m = rq.path.match(/^\/entries\/([0-9a-f]+)$/);
  if (m && rq.method === "GET") {
    const entry = entries.find((x) => x.id === m[1]);
    return entry ? show(rq, entry) : notFound();
  }
  m = rq.path.match(/^\/entries\/([0-9a-f]+)\/delete$/);
  if (m && rq.method === "POST") {
    if (!rq.tokenOk()) return expired();
    const left = entries.filter((x) => x.id !== m[1]);
    if (left.length === entries.length) return notFound();
    saveEntries(left);
    return redirect("/");
  }
  return notFound();
}

const server = http.createServer((req, res) => {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const rq = new Request(req, Buffer.concat(chunks).toString("utf8"));
    const out = route(rq);
    const headers = { ...rq.headers };
    if (out.type) headers["Content-Type"] = out.type;
    if (out.location) headers.Location = out.location;
    res.writeHead(out.status, headers);
    res.end(out.body);
  });
});

server.listen(Number(process.argv[2] || 8080), "127.0.0.1");
