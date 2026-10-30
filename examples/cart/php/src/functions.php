<?php
// The cart library's functions: what a shop prints and computes. Each one has a PHP habit a port must keep.

namespace Cart;

/** Cents as euros the way the shop prints them: "1 234,50 €" (number_format, a space between thousands). */
function format_eur(int $cents): string
{
    return number_format($cents / 100, 2, ',', ' ') . ' €';
}

/** A product name as a URL slug. strtolower changes only A-Z, so "Å" is not a letter here and becomes a dash. */
function slug(string $text): string
{
    return trim(preg_replace('/[^a-z0-9]+/', '-', strtolower($text)), '-');
}

/** The tax in cents on an amount; round() takes a half away from zero, so -0.5 is -1. */
function vat(int $cents, float $rate = 0.24): int
{
    return (int) round($cents * $rate);
}
