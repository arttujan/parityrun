<?php
// A shopping cart: lines by product code, in the order they were added.

namespace Cart;

final class Cart
{
    /** @var array<string, array{qty: int, price: int}> product code => quantity and unit price in cents */
    private array $lines = [];

    public function __construct(public readonly string $currency = 'EUR')
    {
    }

    /** A cart from its JSON: {"currency": "SEK", "lines": {"tea": {"qty": 1, "price": 350}}}. */
    public static function fromJson(string $json): self
    {
        $data = json_decode($json, true, 512, JSON_THROW_ON_ERROR);
        $cart = new self($data['currency'] ?? 'EUR');
        foreach ($data['lines'] ?? [] as $sku => $line) {
            $cart->add((string) $sku, $line['qty'], $line['price']);  // a code like "123" came back as an int key
        }
        return $cart;
    }

    /** Add some of a product (more of one already there adds up); the number of items in the cart. */
    public function add(string $sku, int $qty, int $price): int
    {
        if ($qty <= 0) {
            throw new \InvalidArgumentException("Quantity must be positive, got $qty");
        }
        if (isset($this->lines[$sku])) {
            $this->lines[$sku]['qty'] += $qty;
        } else {
            $this->lines[$sku] = ['qty' => $qty, 'price' => $price];
        }
        return $this->count();
    }

    public function remove(string $sku): void
    {
        if (!isset($this->lines[$sku])) {
            throw new \OutOfBoundsException("No $sku in the cart");
        }
        unset($this->lines[$sku]);
    }

    public function count(): int
    {
        return array_sum(array_column($this->lines, 'qty'));
    }

    public function total(): int
    {
        $sum = 0;
        foreach ($this->lines as $line) {
            $sum += $line['qty'] * $line['price'];
        }
        return $sum;
    }

    /** @return list<array{sku: string, qty: int, price: int}> */
    public function lines(): array
    {
        $out = [];
        foreach ($this->lines as $sku => $line) {
            $out[] = ['sku' => (string) $sku] + $line;
        }
        return $out;
    }

    /** Print the receipt: a line per product, then the total. */
    public function receipt(): void
    {
        foreach ($this->lines() as $line) {
            printf("%-10s %3d x %s\n", $line['sku'], $line['qty'], format_eur($line['price']));
        }
        echo 'Total: ', format_eur($this->total()), "\n";
    }
}
