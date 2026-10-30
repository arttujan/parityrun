//! A small JSON value with a reader and a writer, enough for parityrun's library shim, with no crates.
#![allow(dead_code)]

use std::fmt;

#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    List(Vec<Json>),
    /// An object, its keys in the order they came.
    Map(Vec<(String, Json)>),
}

impl Json {
    pub fn parse(text: &str) -> Result<Json, String> {
        let mut reader = Reader { chars: text.chars().collect(), at: 0 };
        let value = reader.value()?;
        reader.space();
        if reader.at < reader.chars.len() {
            return Err(format!("unexpected text at {}", reader.at));
        }
        Ok(value)
    }

    /// A map's value for `key`.
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Map(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Int(n) => Some(*n),
            Json::Float(f) if f.fract() == 0.0 => Some(*f as i64),
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Json::Int(n) => Some(*n as f64),
            Json::Float(f) => Some(*f),
            _ => None,
        }
    }

    /// The arguments of a call: a list as it is, or named ones (a map) in the order of `names`.
    pub fn args(&self, names: &[&str]) -> Vec<Json> {
        match self {
            Json::List(items) => items.clone(),
            Json::Map(_) => {
                let mut given = names.len();
                while given > 0 && self.get(names[given - 1]).is_none() {
                    given -= 1; // left out at the end: the defaults
                }
                names[..given].iter().map(|n| self.get(n).cloned().unwrap_or(Json::Null)).collect()
            }
            _ => Vec::new(),
        }
    }
}

impl From<&str> for Json {
    fn from(s: &str) -> Json {
        Json::Str(s.to_string())
    }
}

impl From<String> for Json {
    fn from(s: String) -> Json {
        Json::Str(s)
    }
}

impl From<i64> for Json {
    fn from(n: i64) -> Json {
        Json::Int(n)
    }
}

impl From<bool> for Json {
    fn from(b: bool) -> Json {
        Json::Bool(b)
    }
}

struct Reader {
    chars: Vec<char>,
    at: usize,
}

impl Reader {
    fn space(&mut self) {
        while self.at < self.chars.len() && self.chars[self.at].is_whitespace() {
            self.at += 1;
        }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.at).copied()
    }

    fn expect(&mut self, word: &str) -> Result<(), String> {
        for c in word.chars() {
            if self.peek() != Some(c) {
                return Err(format!("expected {word} at {}", self.at));
            }
            self.at += 1;
        }
        Ok(())
    }

    fn value(&mut self) -> Result<Json, String> {
        self.space();
        match self.peek() {
            Some('n') => self.expect("null").map(|_| Json::Null),
            Some('t') => self.expect("true").map(|_| Json::Bool(true)),
            Some('f') => self.expect("false").map(|_| Json::Bool(false)),
            Some('"') => self.string().map(Json::Str),
            Some('[') => {
                self.at += 1;
                let mut items = Vec::new();
                self.space();
                if self.peek() == Some(']') {
                    self.at += 1;
                    return Ok(Json::List(items));
                }
                loop {
                    items.push(self.value()?);
                    self.space();
                    match self.peek() {
                        Some(',') => self.at += 1,
                        Some(']') => {
                            self.at += 1;
                            return Ok(Json::List(items));
                        }
                        _ => return Err(format!("expected , or ] at {}", self.at)),
                    }
                }
            }
            Some('{') => {
                self.at += 1;
                let mut pairs = Vec::new();
                self.space();
                if self.peek() == Some('}') {
                    self.at += 1;
                    return Ok(Json::Map(pairs));
                }
                loop {
                    self.space();
                    let key = self.string()?;
                    self.space();
                    self.expect(":")?;
                    pairs.push((key, self.value()?));
                    self.space();
                    match self.peek() {
                        Some(',') => self.at += 1,
                        Some('}') => {
                            self.at += 1;
                            return Ok(Json::Map(pairs));
                        }
                        _ => return Err(format!("expected , or }} at {}", self.at)),
                    }
                }
            }
            Some(c) if c == '-' || c.is_ascii_digit() => self.number(),
            _ => Err(format!("unexpected text at {}", self.at)),
        }
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.at;
        while let Some(c) = self.peek() {
            if c.is_ascii_digit() || "+-.eE".contains(c) {
                self.at += 1;
            } else {
                break;
            }
        }
        let text: String = self.chars[start..self.at].iter().collect();
        if let Ok(n) = text.parse::<i64>() {
            return Ok(Json::Int(n));
        }
        text.parse::<f64>().map(Json::Float).map_err(|_| format!("not a number: {text}"))
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect("\"")?;
        let mut out = String::new();
        loop {
            match self.peek() {
                None => return Err("unterminated string".to_string()),
                Some('"') => {
                    self.at += 1;
                    return Ok(out);
                }
                Some('\\') => {
                    self.at += 1;
                    let c = self.peek().ok_or("unterminated string")?;
                    self.at += 1;
                    match c {
                        'n' => out.push('\n'),
                        't' => out.push('\t'),
                        'r' => out.push('\r'),
                        'b' => out.push('\u{8}'),
                        'f' => out.push('\u{c}'),
                        'u' => {
                            let mut code = self.hex()?;
                            if (0xD800..0xDC00).contains(&code) && self.peek() == Some('\\') {
                                self.at += 2; // \u of the low half
                                code = 0x10000 + ((code - 0xD800) << 10) + (self.hex()? - 0xDC00);
                            }
                            out.push(char::from_u32(code).unwrap_or('\u{FFFD}'));
                        }
                        other => out.push(other),
                    }
                }
                Some(c) => {
                    out.push(c);
                    self.at += 1;
                }
            }
        }
    }

    fn hex(&mut self) -> Result<u32, String> {
        let digits: String = self.chars.get(self.at..self.at + 4).ok_or("short \\u escape")?.iter().collect();
        self.at += 4;
        u32::from_str_radix(&digits, 16).map_err(|_| format!("bad \\u escape: {digits}"))
    }
}

fn write_str(f: &mut fmt::Formatter, s: &str) -> fmt::Result {
    f.write_str("\"")?;
    for c in s.chars() {
        match c {
            '"' => f.write_str("\\\"")?,
            '\\' => f.write_str("\\\\")?,
            '\n' => f.write_str("\\n")?,
            '\r' => f.write_str("\\r")?,
            '\t' => f.write_str("\\t")?,
            c if (c as u32) < 0x20 => write!(f, "\\u{:04x}", c as u32)?,
            c => write!(f, "{c}")?,
        }
    }
    f.write_str("\"")
}

impl fmt::Display for Json {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Json::Null => f.write_str("null"),
            Json::Bool(b) => write!(f, "{b}"),
            Json::Int(n) => write!(f, "{n}"),
            Json::Float(x) if x.is_finite() => write!(f, "{x:?}"),
            Json::Float(_) => f.write_str("null"),
            Json::Str(s) => write_str(f, s),
            Json::List(items) => {
                f.write_str("[")?;
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        f.write_str(",")?;
                    }
                    write!(f, "{item}")?;
                }
                f.write_str("]")
            }
            Json::Map(pairs) => {
                f.write_str("{")?;
                for (i, (k, v)) in pairs.iter().enumerate() {
                    if i > 0 {
                        f.write_str(",")?;
                    }
                    write_str(f, k)?;
                    write!(f, ":{v}")?;
                }
                f.write_str("}")
            }
        }
    }
}
