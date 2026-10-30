// parityrun's library shim for a JavaScript port: serves the port's functions and classes over HTTP under the
// original library's names, answering exactly as the original's shim does, for the scenarios' `fn` and `new` steps.
//
//   POST /call  {"fn": "name" | "Class::method" | "object.method", "args": [...] or {...named}, "as": "object"}
//               {"new": "Class", "args": [...], "as": "object"}
//               -> {"value": ...} or {"error": {"class": ..., "message": ...}}, and "output" when it printed
//   GET /health -> 200
//
// Fill in the three tables below; the rest is the protocol and stays as it is.
"use strict";

const http = require("node:http");
const lib = require("./index.js");

// The original's functions and static methods -> the port's. `args` is a list, or an object of named arguments:
// named(args, ["cents", "rate"]) turns either into a list.
const FUNCTIONS = {
  // "Shop\\slug": (args) => lib.slug(...named(args, ["text"])),
  // "Shop\\Cart::fromJson": (args) => lib.Cart.fromJson(...named(args, ["json"])),
};

// The original's classes -> the port's: what `new` makes, and the name a value of that class is recorded under.
const CLASSES = {
  // "Shop\\Cart": lib.Cart,
};

// The port's errors -> the original's exception classes (by the port's error class name). An error not listed is
// recorded under its own name.
const ERRORS = {
  // InvalidQuantity: "InvalidArgumentException",
};

function named(args, names) {
  if (Array.isArray(args)) return args;
  const out = names.map((name) => args[name]);
  while (out.length && out[out.length - 1] === undefined) out.pop(); // left out: the default
  return out;
}

const CLASS_NAMES = new Map(Object.entries(CLASSES).map(([name, cls]) => [cls, name]));

// A value as the original's shim records it: an object of a class is {"$object": class, its public fields}.
function data(value) {
  if (value === undefined || value === null) return null;
  if (Array.isArray(value)) return value.map(data);
  if (typeof value === "number" && !Number.isFinite(value)) {
    return Number.isNaN(value) ? "NAN" : value > 0 ? "INF" : "-INF";
  }
  if (typeof value === "object") {
    const out = {};
    const cls = CLASS_NAMES.get(value.constructor);
    if (cls) out.$object = cls;
    else if (value.constructor && value.constructor !== Object) out.$object = value.constructor.name;
    for (const [k, v] of Object.entries(value)) out[k] = data(v);
    return out;
  }
  return value;
}

const objects = new Map();

// A mistake in the scenario or the tables, not the library's behaviour: the shim answers 400.
const protocol = (message) => Object.assign(new Error(message), { protocol: true });

function call(request) {
  const args = request.args ?? [];
  if (request.new) {
    const cls = CLASSES[request.new];
    if (!cls) throw protocol(`no class ${request.new} in the shim's CLASSES`);
    return new cls(...named(args, []));
  }
  const m = /^([A-Za-z_]\w*)\.(\w+)$/.exec(request.fn ?? "");
  if (m) {
    if (!objects.has(m[1])) throw protocol(`no object called ${m[1]}: give a step \`as: ${m[1]}\` first`);
    const obj = objects.get(m[1]);
    return obj[m[2]](...named(args, []));
  }
  const fn = FUNCTIONS[request.fn];
  if (!fn) throw protocol(`no function ${request.fn} in the shim's FUNCTIONS`);
  return fn(args);
}

function answer(request) {
  let output = "";
  const write = process.stdout.write;
  process.stdout.write = (chunk) => { output += String(chunk); return true; }; // what the call prints
  let result;
  try {
    const value = call(request);
    result = { value: data(value) };
    if (request.as) objects.set(request.as, value);
  } catch (err) {
    if (err && err.protocol) throw err;
    const name = err && err.constructor ? err.constructor.name : "Error";
    const message = err && err.message !== undefined ? err.message : String(err);
    result = { error: { class: ERRORS[name] ?? name, message } };
  } finally {
    process.stdout.write = write;
  }
  if (output !== "") result.output = output;
  return result;
}

http.createServer((req, res) => {
  let body = "";
  req.setEncoding("utf8");
  req.on("data", (chunk) => { body += chunk; });
  req.on("end", () => {
    let status = 200;
    let reply;
    if (req.method === "GET" && req.url === "/health") reply = "ok";
    else if (req.method === "POST" && req.url === "/call") {
      try {
        reply = answer(JSON.parse(body));
      } catch (err) {
        status = 400;
        reply = { error: String(err && err.message) };
      }
    } else {
      status = 404;
      reply = { error: `no ${req.method} ${req.url}: POST /call` };
    }
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(reply));
  });
}).listen(Number(process.env.PORT), "127.0.0.1");
