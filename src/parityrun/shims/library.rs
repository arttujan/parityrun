// parityrun's library shim for a Rust port: serves the port's functions and types over HTTP under the original
// library's names, answering exactly as the original's shim does, for the scenarios' `fn` and `new` steps.
//
//   POST /call  {"fn": "name" | "Class::method" | "object.method", "args": [...] or {...named}, "as": "object"}
//               {"new": "Class", "args": [...], "as": "object"}
//               -> {"value": ...} or {"error": {"class": ..., "message": ...}}, and "output" when it printed
//   GET /health -> 200
//
// Fill in `Object`, `call` and `data`; the rest is the protocol and stays as it is.

mod json;

use json::Json;
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};

#[allow(unused_imports)]
use {crate} as lib;

/// The library's objects a step can keep (`as`) and call methods on: one variant per type.
pub enum Object {
    // Cart(lib::Cart),
}

/// What a call gives: plain data, or one of the library's objects.
pub enum Value {
    Data(Json),
    Object(Object),
}

/// A call that didn't give a value.
pub enum Failure {
    /// The library failed, as the original throws: its exception class and message.
    Thrown { class: String, message: String },
    /// A mistake in the scenario or in `call`, not the library's behaviour: the shim answers 400.
    Unknown(String),
}

/// One call, under the original's names: `new` is the class of a `new` step; else `name` is a function or a static
/// method ("Shop\\slug", "Shop\\Cart::fromJson") or, with `object`, a method on it ("add"). `args` is a list, or
/// named arguments: `args.args(&["cents", "rate"])` gives either as a list. What the library prints goes to `out`.
#[allow(unused_variables)]
fn call(new: Option<&str>, name: &str, object: Option<&mut Object>, args: &Json, out: &mut String)
        -> Result<Value, Failure> {
    match (new, name, object) {
        // (Some("Shop\\Cart"), _, _) => Ok(Value::Object(Object::Cart(lib::Cart::new()))),
        // (None, "Shop\\slug", None) => Ok(Value::Data(lib::slug(text(&args.args(&["text"]), 0)?).into())),
        // (None, "add", Some(Object::Cart(cart))) => ...,
        (Some(class), _, _) => Err(Failure::Unknown(format!("no class {class} in the shim's call()"))),
        (None, name, _) => Err(Failure::Unknown(format!("no function or method {name} in the shim's call()"))),
    }
}

/// An object as the original's shim records it: {"$object": "Shop\\Cart", and its public properties}.
fn data(object: &Object) -> Json {
    match *object {
        // Object::Cart(ref cart) => Json::Map(vec![("$object".into(), "Shop\\Cart".into()), ...]),
    }
}

/// The argument at `i` as an integer, a number or text.
#[allow(dead_code)]
fn int(args: &[Json], i: usize) -> Result<i64, Failure> {
    args.get(i).and_then(Json::as_i64).ok_or_else(|| Failure::Unknown(format!("argument {i} isn't an integer")))
}

#[allow(dead_code)]
fn number(args: &[Json], i: usize) -> Result<f64, Failure> {
    args.get(i).and_then(Json::as_f64).ok_or_else(|| Failure::Unknown(format!("argument {i} isn't a number")))
}

#[allow(dead_code)]
fn text(args: &[Json], i: usize) -> Result<&str, Failure> {
    args.get(i).and_then(Json::as_str).ok_or_else(|| Failure::Unknown(format!("argument {i} isn't text")))
}

fn answer(request: &Json, objects: &mut HashMap<String, Object>) -> Result<Json, String> {
    let empty = Json::List(Vec::new());
    let args = request.get("args").unwrap_or(&empty);
    let name = request.get("fn").and_then(Json::as_str).unwrap_or("");
    let new = request.get("new").and_then(Json::as_str);
    let mut out = String::new();
    let on_object = name.split_once('.')  // "cart.add": a method on a kept object
        .filter(|(o, _)| !o.is_empty() && o.chars().all(|c| c.is_alphanumeric() || c == '_'));
    let result = match (new, on_object) {
        (Some(_), _) => call(new, "", None, args, &mut out),
        (None, Some((obj, method))) => {
            let object = objects.get_mut(obj).ok_or(format!("no object called {obj}: give a step `as: {obj}` first"))?;
            call(None, method, Some(object), args, &mut out)
        }
        (None, None) => call(None, name, None, args, &mut out),
    };
    let mut reply: Vec<(String, Json)> = match result {
        Ok(Value::Data(value)) => vec![("value".into(), value)],
        Ok(Value::Object(object)) => {
            let value = data(&object);
            if let Some(keep) = request.get("as").and_then(Json::as_str) {
                objects.insert(keep.to_string(), object);
            }
            vec![("value".into(), value)]
        }
        Err(Failure::Thrown { class, message }) => {
            vec![("error".into(), Json::Map(vec![("class".into(), class.into()), ("message".into(), message.into())]))]
        }
        Err(Failure::Unknown(why)) => return Err(why),
    };
    if !out.is_empty() {
        reply.push(("output".into(), out.into()));
    }
    Ok(Json::Map(reply))
}

fn serve(stream: TcpStream, objects: &mut HashMap<String, Object>) {
    let mut reader = BufReader::new(stream);
    let mut first = String::new();
    if reader.read_line(&mut first).is_err() {
        return;
    }
    let mut length = 0;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).unwrap_or(0) == 0 || line.trim_end().is_empty() {
            break;
        }
        if let Some((key, value)) = line.split_once(':') {
            if key.eq_ignore_ascii_case("content-length") {
                length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body = vec![0u8; length];
    if reader.read_exact(&mut body).is_err() {
        return;
    }
    let mut parts = first.split_whitespace();
    let (method, path) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""));
    let (status, reply) = match (method, path) {
        ("GET", "/health") => ("200 OK", Json::from("ok")),
        ("POST", "/call") => match Json::parse(&String::from_utf8_lossy(&body)).and_then(|r| answer(&r, objects)) {
            Ok(value) => ("200 OK", value),
            Err(why) => ("400 Bad Request", Json::Map(vec![("error".into(), why.into())])),
        },
        _ => ("404 Not Found", Json::Map(vec![("error".into(), format!("no {method} {path}: POST /call").into())])),
    };
    let text = reply.to_string();
    let mut stream = reader.into_inner();
    let _ = write!(stream, "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                           Connection: close\r\n\r\n{text}", text.len());
}

fn main() {
    let port = std::env::var("PORT").expect("parityrun gives the port in PORT");
    let listener = TcpListener::bind(format!("127.0.0.1:{port}")).expect("port in use");
    let mut objects = HashMap::new();
    for stream in listener.incoming().flatten() {
        serve(stream, &mut objects);
    }
}
