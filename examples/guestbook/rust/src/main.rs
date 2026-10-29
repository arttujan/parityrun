//! The guestbook ported to Rust (std only, no crates): it must behave exactly like ../php/index.php.
//!
//!   cargo build --release && target/release/guestbook 8080        (GUESTBOOK_DATA: the data folder)

use std::collections::hash_map::RandomState;
use std::collections::HashMap;
use std::hash::{BuildHasher, Hasher};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

const NAME_MAX: usize = 40;
const MESSAGE_MAX: usize = 200;

#[derive(Clone)]
struct Entry {
    id: String,
    name: String,
    message: String,
    at: String,
}

struct Answer {
    status: u16,
    body: String,
    content_type: Option<&'static str>,
    location: Option<String>,
}

struct Visit {
    method: String,
    path: String,
    cookies: HashMap<String, String>,
    form: Vec<(String, String)>,
    made: Option<String>,
}

/// htmlspecialchars(ENT_QUOTES): a single quote is &#039;
fn e(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#039;"),
            _ => out.push(c),
        }
    }
    out
}

/// PHP's trim(): only these, not Unicode spaces (a no-break space stays).
fn php_trim(s: &str) -> String {
    s.trim_matches(|c| matches!(c, ' ' | '\t' | '\n' | '\r' | '\0' | '\x0B')).to_string()
}

fn nl2br(s: &str) -> String {
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '\n' || c == '\r' {
            out.push_str("<br />");
            out.push(c);
            let next = chars.get(i + 1).copied();
            if (c == '\r' && next == Some('\n')) || (c == '\n' && next == Some('\r')) {
                out.push(next.unwrap());
                i += 1;
            }
        } else {
            out.push(c);
        }
        i += 1;
    }
    out
}

fn random_hex(bytes: usize) -> String {
    let mut out = String::new();
    while out.len() < bytes * 2 {
        let mut h = RandomState::new().build_hasher();
        h.write_u128(SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos());
        h.write_usize(out.len());
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out.truncate(bytes * 2);
    out
}

/// Days since 1970-01-01 to (year, month, day): Howard Hinnant's civil_from_days.
fn civil(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

/// gmdate('c'): 2026-10-29T10:00:00+00:00
fn now() -> String {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
    let (y, m, d) = civil(secs.div_euclid(86_400));
    let t = secs.rem_euclid(86_400);
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}+00:00", t / 3600, t / 60 % 60, t % 60)
}

/// "2026-10-29T10:00:00+00:00" -> "2026-10-29 10:00" (entries are always written in UTC)
fn minute(at: &str) -> String {
    at.get(..16).unwrap_or(at).replacen('T', " ", 1)
}

fn percent_decode(s: &str) -> String {
    let bytes = s.replace('+', " ").into_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(v) = u8::from_str_radix(std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or(""), 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn json_string(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

struct Store {
    dir: PathBuf,
    entries: Vec<Entry>,
}

impl Store {
    /// Entries live in memory (one process per data folder) and are written to entries.json after each change.
    fn save(&self) {
        std::fs::create_dir_all(&self.dir).unwrap();
        let items: Vec<String> = self
            .entries
            .iter()
            .map(|x| {
                format!(
                    "    {{\n        \"id\": {},\n        \"name\": {},\n        \"message\": {},\n        \"at\": {}\n    }}",
                    json_string(&x.id),
                    json_string(&x.name),
                    json_string(&x.message),
                    json_string(&x.at)
                )
            })
            .collect();
        let text = if items.is_empty() { "[]".to_string() } else { format!("[\n{}\n]", items.join(",\n")) };
        std::fs::write(self.dir.join("entries.json"), text).unwrap();
    }
}

impl Visit {
    /// PHP's $_POST: the last value of a repeated field.
    fn field(&self, name: &str) -> String {
        self.form.iter().rev().find(|(k, _)| k == name).map(|(_, v)| v.clone()).unwrap_or_default()
    }

    fn token(&mut self) -> String {
        if let Some(t) = self.cookies.get("csrf").filter(|t| !t.is_empty()) {
            return t.clone();
        }
        self.made.get_or_insert_with(|| random_hex(16)).clone()
    }

    fn token_ok(&self) -> bool {
        let sent = self.field("csrf");
        matches!(self.cookies.get("csrf"), Some(c) if !c.is_empty() && !sent.is_empty() && *c == sent)
    }
}

fn page(status: u16, title: &str, body: &str) -> Answer {
    let html = format!(
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n  <title>{} · Guestbook</title>\n\
         </head>\n<body>\n  <h1><a href=\"/\">Guestbook</a></h1>\n{}</body>\n</html>\n",
        e(title),
        body
    );
    Answer { status, body: html, content_type: Some("text/html; charset=utf-8"), location: None }
}

fn redirect(to: String) -> Answer {
    Answer { status: 303, body: String::new(), content_type: None, location: Some(to) }
}

fn not_found() -> Answer {
    page(404, "Not found", "  <p>Page not found.</p>\n")
}

fn expired() -> Answer {
    page(403, "Expired", "  <p>Your session expired. Reload the page and try again.</p>\n")
}

fn index(v: &mut Visit, entries: &[Entry], errors: &[String], name: &str, message: &str, status: u16) -> Answer {
    let mut body = String::from("  <form method=\"post\" action=\"/sign\">\n");
    body += &format!("    <input type=\"hidden\" name=\"csrf\" value=\"{}\">\n", e(&v.token()));
    if !errors.is_empty() {
        body += "    <ul class=\"errors\">\n";
        for error in errors {
            body += &format!("      <li>{}</li>\n", e(error));
        }
        body += "    </ul>\n";
    }
    body += &format!("    <label>Name <input name=\"name\" maxlength=\"{NAME_MAX}\" value=\"{}\"></label>\n", e(name));
    body += &format!("    <label>Message <textarea name=\"message\">{}</textarea></label>\n", e(message));
    body += "    <button>Sign</button>\n  </form>\n";
    body += &format!("  <h2>{}{}</h2>\n", entries.len(), if entries.len() == 1 { " entry" } else { " entries" });
    if entries.is_empty() {
        body += "  <p class=\"empty\">No entries yet. Be the first!</p>\n";
    } else {
        body += "  <ul class=\"entries\">\n";
        for entry in entries.iter().rev() {
            body += &format!(
                "    <li><a href=\"/entries/{}\">{}</a>: {}</li>\n",
                e(&entry.id),
                e(&entry.name),
                e(&entry.message)
            );
        }
        body += "  </ul>\n";
    }
    page(status, "Sign the guestbook", &body)
}

fn show(v: &mut Visit, entry: &Entry) -> Answer {
    let mut body = format!("  <article>\n    <h2>{}</h2>\n", e(&entry.name));
    body += &format!("    <p>{}</p>\n", nl2br(&e(&entry.message)));
    body += &format!("    <p class=\"meta\">Signed {} UTC</p>\n", minute(&entry.at));
    body += &format!("    <form method=\"post\" action=\"/entries/{}/delete\">\n", e(&entry.id));
    body += &format!("      <input type=\"hidden\" name=\"csrf\" value=\"{}\">\n", e(&v.token()));
    body += "      <button>Delete</button>\n    </form>\n  </article>\n";
    page(200, &entry.name, &body)
}

fn sign(v: &mut Visit, store: &mut Store) -> Answer {
    if !v.token_ok() {
        return expired();
    }
    let name = php_trim(&v.field("name"));
    let message = php_trim(&v.field("message"));
    let mut errors = Vec::new();
    if name.is_empty() {
        errors.push("Please write your name.".to_string());
    } else if name.chars().count() > NAME_MAX {
        errors.push(format!("Your name can be at most {NAME_MAX} characters."));
    }
    if message.is_empty() {
        errors.push("Please write a message.".to_string());
    } else if message.chars().count() > MESSAGE_MAX {
        errors.push(format!("Your message can be at most {MESSAGE_MAX} characters."));
    }
    if !errors.is_empty() {
        return index(v, &store.entries, &errors, &name, &message, 422);
    }
    let entry = Entry { id: random_hex(4), name, message, at: now() };
    let to = format!("/entries/{}", entry.id);
    store.entries.push(entry);
    store.save();
    redirect(to)
}

fn entry_id<'a>(path: &'a str, suffix: &str) -> Option<&'a str> {
    let id = path.strip_prefix("/entries/")?.strip_suffix(suffix)?;
    (!id.is_empty() && id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))).then_some(id)
}

fn route(v: &mut Visit, store: &mut Store) -> Answer {
    let (method, path) = (v.method.clone(), v.path.clone());
    if method == "GET" && path == "/health" {
        return Answer { status: 200, body: "ok".into(), content_type: Some("text/plain; charset=utf-8"), location: None };
    }
    if method == "GET" && path == "/" {
        return index(v, &store.entries, &[], "", "", 200);
    }
    if method == "POST" && path == "/sign" {
        return sign(v, store);
    }
    if let (Some(id), "GET") = (entry_id(&path, ""), method.as_str()) {
        return match store.entries.iter().find(|x| x.id == id).cloned() {
            Some(entry) => show(v, &entry),
            None => not_found(),
        };
    }
    if let (Some(id), "POST") = (entry_id(&path, "/delete"), method.as_str()) {
        if !v.token_ok() {
            return expired();
        }
        let before = store.entries.len();
        store.entries.retain(|x| x.id != id);
        if store.entries.len() == before {
            return not_found();
        }
        store.save();
        return redirect("/".into());
    }
    not_found()
}

fn read_visit(stream: &mut TcpStream) -> Option<Visit> {
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).ok()?;
    let mut parts = line.split_whitespace();
    let method = parts.next()?.to_string();
    let target = parts.next()?.to_string();
    let mut headers = HashMap::new();
    loop {
        let mut h = String::new();
        if reader.read_line(&mut h).ok()? == 0 || h.trim_end().is_empty() {
            break;
        }
        if let Some((k, val)) = h.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), val.trim().to_string());
        }
    }
    let length: usize = headers.get("content-length").and_then(|n| n.parse().ok()).unwrap_or(0);
    let mut body = vec![0; length];
    reader.read_exact(&mut body).ok()?;
    let mut cookies = HashMap::new();
    for part in headers.get("cookie").map(String::as_str).unwrap_or("").split(';') {
        if let Some((k, val)) = part.split_once('=') {
            cookies.insert(k.trim().to_string(), percent_decode(val.trim()));
        }
    }
    let form = if headers.get("content-type").map(String::as_str) == Some("application/x-www-form-urlencoded") {
        String::from_utf8_lossy(&body)
            .split('&')
            .filter(|p| !p.is_empty())
            .map(|p| {
                let (k, val) = p.split_once('=').unwrap_or((p, ""));
                (percent_decode(k), percent_decode(val))
            })
            .collect()
    } else {
        Vec::new()
    };
    let path = target.split('?').next().unwrap_or("/").to_string();
    Some(Visit { method, path, cookies, form, made: None })
}

fn main() {
    let port = std::env::args().nth(1).unwrap_or_else(|| "8080".into());
    let dir = std::env::var("GUESTBOOK_DATA").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from("data"));
    let mut store = Store { dir, entries: Vec::new() };
    let listener = TcpListener::bind(format!("127.0.0.1:{port}")).expect("port in use");
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { continue };
        let Some(mut visit) = read_visit(&mut stream) else { continue };
        let answer = route(&mut visit, &mut store);
        let mut head = format!("HTTP/1.1 {} {}\r\n", answer.status, reason(answer.status));
        if let Some(t) = answer.content_type {
            head += &format!("Content-Type: {t}\r\n");
        }
        if let Some(to) = &answer.location {
            head += &format!("Location: {to}\r\n");
        }
        if let Some(made) = &visit.made {
            head += &format!("Set-Cookie: csrf={made}; path=/; HttpOnly; SameSite=Lax\r\n");
        }
        head += &format!("Content-Length: {}\r\nConnection: close\r\n\r\n", answer.body.len());
        let _ = stream.write_all(head.as_bytes());
        let _ = stream.write_all(answer.body.as_bytes());
        let _ = stream.flush();
    }
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        303 => "See Other",
        403 => "Forbidden",
        404 => "Not Found",
        422 => "Unprocessable Content",
        _ => "Unknown",
    }
}
