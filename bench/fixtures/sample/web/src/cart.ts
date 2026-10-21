/** One line of a shopping cart. */
export interface CartLine {
  sku: string;
  name: string;
  priceCents: number;
  quantity: number;
}

/** Return the cart total in cents: the sum of price times quantity. */
export function cartTotal(lines: readonly CartLine[]): number {
  return lines.reduce((sum, line) => sum + line.priceCents * line.quantity, 0);
}

/** Format cents as a price with two decimals, e.g. 1234 -> "12.34". */
export function formatCents(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

/** Return a copy of the cart with `line` added, merging quantities by SKU. */
export function addLine(lines: readonly CartLine[], line: CartLine): CartLine[] {
  const existing = lines.find((l) => l.sku === line.sku);
  if (!existing) {
    return [...lines, line];
  }
  return lines.map((l) =>
    l.sku === line.sku ? { ...l, quantity: l.quantity + line.quantity } : l,
  );
}
