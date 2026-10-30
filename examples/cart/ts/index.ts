// The cart library ported to TypeScript (node 23.6+ runs it as is, no build, no packages): it must behave exactly
// like ../php/src.

export class InvalidArgumentException extends Error {}
export class OutOfBoundsException extends Error {}
export class JsonException extends Error {}

type Line = { qty: number; price: number };

// number_format($cents / 100, 2, ',', ' ') . ' €': whole cents, so no float rounding to copy
export function formatEur(cents: number): string {
  const abs = Math.abs(cents);
  const whole = String(Math.floor(abs / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  return `${cents < 0 ? "-" : ""}${whole},${String(abs % 100).padStart(2, "0")} €`;
}

// strtolower changes only A-Z (not "Å" or "Ö"), unlike String.toLowerCase
export function slug(text: string): string {
  const lower = text.replace(/[A-Z]/g, (c) => c.toLowerCase());
  return lower.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

// round() takes a half away from zero; Math.round takes it up (-0.5 -> -0)
export function vat(cents: number, rate: number = 0.24): number {
  const x = cents * rate;
  return Math.sign(x) * Math.round(Math.abs(x)) || 0;
}

export class Cart {
  #lines = new Map<string, Line>(); // product code -> quantity and unit price, in the order added

  constructor(currency: string = "EUR") {
    Object.defineProperty(this, "currency", { value: currency, enumerable: true }); // public readonly
  }

  static fromJson(json: string): Cart {
    let data: { currency?: string; lines?: Record<string, Line> };
    try {
      data = JSON.parse(json);
    } catch {
      throw new JsonException("Syntax error"); // json_decode's message for any broken JSON
    }
    const cart = new Cart(data.currency ?? "EUR");
    for (const [sku, line] of Object.entries(data.lines ?? {})) cart.add(String(sku), line.qty, line.price);
    return cart;
  }

  add(sku: string, qty: number, price: number): number {
    if (qty <= 0) throw new InvalidArgumentException(`Quantity must be positive, got ${qty}`);
    const line = this.#lines.get(sku);
    if (line) line.qty += qty;
    else this.#lines.set(sku, { qty, price });
    return this.count();
  }

  remove(sku: string): void {
    if (!this.#lines.has(sku)) throw new OutOfBoundsException(`No ${sku} in the cart`);
    this.#lines.delete(sku);
  }

  count(): number {
    let n = 0;
    for (const line of this.#lines.values()) n += line.qty;
    return n;
  }

  total(): number {
    let sum = 0;
    for (const line of this.#lines.values()) sum += line.qty * line.price;
    return sum;
  }

  lines(): { sku: string; qty: number; price: number }[] {
    return [...this.#lines].map(([sku, line]) => ({ sku, qty: line.qty, price: line.price }));
  }

  // printf("%-10s %3d x %s\n", ...) per line, then the total
  receipt(): void {
    for (const line of this.lines()) {
      process.stdout.write(`${line.sku.padEnd(10)} ${String(line.qty).padStart(3)} x ${formatEur(line.price)}\n`);
    }
    process.stdout.write(`Total: ${formatEur(this.total())}\n`);
  }
}
