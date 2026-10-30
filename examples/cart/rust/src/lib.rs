//! The cart library ported to Rust (std only): it must behave exactly like ../php/src.

mod json;

use json::Json;

/// What the PHP library throws: each is reported under PHP's exception class.
#[derive(Debug)]
pub enum CartError {
    InvalidArgument(String),
    OutOfBounds(String),
    Json(String),
}

/// number_format($cents / 100, 2, ',', ' ') . ' €': whole cents, so no float rounding to copy.
pub fn format_eur(cents: i64) -> String {
    let abs = cents.unsigned_abs();
    let whole = (abs / 100).to_string();
    let mut grouped = String::new();
    for (i, c) in whole.chars().enumerate() {
        if i > 0 && (whole.len() - i) % 3 == 0 {
            grouped.push(' ');
        }
        grouped.push(c);
    }
    format!("{}{},{:02} €", if cents < 0 { "-" } else { "" }, grouped, abs % 100)
}

/// strtolower changes only A-Z, so "Å" is not a letter here: every run of anything but a-z and 0-9 is one dash,
/// none at either end.
pub fn slug(text: &str) -> String {
    let mut out = String::new();
    let mut dash = false;
    for c in text.chars().map(|c| c.to_ascii_lowercase()) {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            if dash && !out.is_empty() {
                out.push('-');
            }
            dash = false;
            out.push(c);
        } else {
            dash = true;
        }
    }
    out
}

/// round() takes a half away from zero, as f64::round does.
pub fn vat(cents: i64, rate: f64) -> i64 {
    (cents as f64 * rate).round() as i64
}

struct Line {
    sku: String,
    qty: i64,
    price: i64,
}

pub struct Cart {
    pub currency: String,
    lines: Vec<Line>, // in the order added
}

impl Cart {
    pub fn new(currency: &str) -> Cart {
        Cart { currency: currency.to_string(), lines: Vec::new() }
    }

    /// A cart from its JSON: {"currency": "SEK", "lines": {"tea": {"qty": 1, "price": 350}}}.
    pub fn from_json(text: &str) -> Result<Cart, CartError> {
        let data = Json::parse(text).map_err(|_| CartError::Json("Syntax error".to_string()))?;
        let mut cart = Cart::new(data.get("currency").and_then(Json::as_str).unwrap_or("EUR"));
        if let Some(Json::Map(lines)) = data.get("lines") {
            for (sku, line) in lines {
                let field = |key: &str| line.get(key).and_then(Json::as_i64).unwrap_or(0);
                cart.add(sku, field("qty"), field("price"))?;
            }
        }
        Ok(cart)
    }

    /// Add some of a product (more of one already there adds up); the number of items in the cart.
    pub fn add(&mut self, sku: &str, qty: i64, price: i64) -> Result<i64, CartError> {
        if qty <= 0 {
            return Err(CartError::InvalidArgument(format!("Quantity must be positive, got {qty}")));
        }
        match self.lines.iter_mut().find(|line| line.sku == sku) {
            Some(line) => line.qty += qty,
            None => self.lines.push(Line { sku: sku.to_string(), qty, price }),
        }
        Ok(self.count())
    }

    pub fn remove(&mut self, sku: &str) -> Result<(), CartError> {
        let at = self.lines.iter().position(|line| line.sku == sku)
            .ok_or_else(|| CartError::OutOfBounds(format!("No {sku} in the cart")))?;
        self.lines.remove(at);
        Ok(())
    }

    pub fn count(&self) -> i64 {
        self.lines.iter().map(|line| line.qty).sum()
    }

    pub fn total(&self) -> i64 {
        self.lines.iter().map(|line| line.qty * line.price).sum()
    }

    /// (product code, quantity, unit price) per line.
    pub fn lines(&self) -> Vec<(String, i64, i64)> {
        self.lines.iter().map(|line| (line.sku.clone(), line.qty, line.price)).collect()
    }

    /// The receipt PHP prints with printf("%-10s %3d x %s\n", ...) per line, then the total.
    pub fn receipt(&self, out: &mut String) {
        for line in &self.lines {
            out.push_str(&format!("{:<10} {:>3} x {}\n", line.sku, line.qty, format_eur(line.price)));
        }
        out.push_str(&format!("Total: {}\n", format_eur(self.total())));
    }
}
