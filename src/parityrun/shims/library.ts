// parityrun's library shim for a TypeScript port: serves the port's functions and classes over HTTP under the
// original library's names, answering exactly as the original's shim does, for the scenarios' `fn` and `new` steps.
//
//   POST /call  {"fn": "name" | "Class::method" | "object.method", "args": [...] or {...named}, "as": "object"}
//               {"new": "Class", "args": [...], "as": "object"}
//               -> {"value": ...} or {"error": {"class": ..., "message": ...}}, and "output" when it printed
//   GET /health -> 200
//
// Fill in the three tables below; the rest is the protocol and stays as it is. node 23.6+ runs it as it is.
import http from "node:http";
import * as lib from "./index.ts";

type Args = unknown[] | Record<string, unknown>;

// The original's functions and static methods -> the port's. `args` is a list, or an object of named arguments:
// named(args, ["cents", "rate"]) turns either into a list.
const FUNCTIONS: Record<string, (args: Args) => unknown> = {
  // "Shop\\slug": (args) => lib.slug(...(named(args, ["text"]) as [string])),
};

// The original's classes -> the port's: what `new` makes, and the name a value of that class is recorded under.
const CLASSES: Record<string, new (...args: any[]) => object> = {
  // "Shop\\Cart": lib.Cart,
};

// The port's errors -> the original's exception classes (by the port's error class name). An error not listed is
// recorded under its own name.
const ERRORS: Record<string, string> = {
  // InvalidQuantity: "InvalidArgumentException",
};

function named(args: Args, names: string[]): unknown[] {
  if (Array.isArray(args)) return args;
  const out = names.map((name) => args[name]);
  while (out.length && out[out.length - 1] === undefined) out.pop(); // left out: the default
  return out;
}

const CLASS_NAMES = new Map<unknown, string>(Object.entries(CLASSES).map(([name, cls]) => [cls, name]));

// A value as the original's shim records it: an object of a class is {"$object": class, its public fields}.
function data(value: unknown): unknown {
  if (value === undefined || value === null) return null;
  if (Array.isArray(value)) return value.map(data);
  if (typeof value === "number" && !Number.isFinite(value)) {
    return Number.isNaN(value) ? "NAN" : value > 0 ? "INF" : "-INF";
  }
  if (typeof value === "object") {
    const out: Record<string, unknown> = {};
    const ctor = (value as object).constructor;
    const cls = CLASS_NAMES.get(ctor);
    if (cls) out.$object = cls;
    else if (ctor && ctor !== Object) out.$object = ctor.name;
    for (const [k, v] of Object.entries(value as object)) out[k] = data(v);
    return out;
  }
  return value;
}

type Request = { fn?: string; new?: string; args?: Args; as?: string };

const objects = new Map<string, any>();

// A mistake in the scenario or the tables, not the library's behaviour: the shim answers 400.
const protocol = (message: string) => Object.assign(new Error(message), { protocol: true });

function call(request: Request): unknown {
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
  const fn = FUNCTIONS[request.fn ?? ""];
  if (!fn) throw protocol(`no function ${request.fn} in the shim's FUNCTIONS`);
  return fn(args);
}

function answer(request: Request): Record<string, unknown> {
  let output = "";
  const write = process.stdout.write;
  (process.stdout as any).write = (chunk: unknown) => { output += String(chunk); return true; }; // what it prints
  let result: Record<string, unknown>;
  try {
    const value = call(request);
    result = { value: data(value) };
    if (request.as) objects.set(request.as, value);
  } catch (err: any) {
    if (err && err.protocol) throw err;
    const name: string = err && err.constructor ? err.constructor.name : "Error";
    const message = err && err.message !== undefined ? err.message : String(err);
    result = { error: { class: ERRORS[name] ?? name, message } };
  } finally {
    process.stdout.write = write;
  }
  if (output !== "") result.output = output;
  return result;
}

http.createServer((req: http.IncomingMessage, res: http.ServerResponse) => {
  let body = "";
  req.setEncoding("utf8");
  req.on("data", (chunk: string) => { body += chunk; });
  req.on("end", () => {
    let status = 200;
    let reply: unknown;
    if (req.method === "GET" && req.url === "/health") reply = "ok";
    else if (req.method === "POST" && req.url === "/call") {
      try {
        reply = answer(JSON.parse(body));
      } catch (err: any) {
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
