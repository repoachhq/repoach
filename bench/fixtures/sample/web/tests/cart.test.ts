import { describe, expect, it } from "vitest";

import { addLine, cartTotal, formatCents } from "../src/cart";

describe("cart", () => {
  it("totals price times quantity", () => {
    expect(
      cartTotal([
        { sku: "p1", name: "pen", priceCents: 150, quantity: 2 },
        { sku: "p2", name: "pad", priceCents: 300, quantity: 1 },
      ]),
    ).toBe(600);
  });

  it("formats cents", () => {
    expect(formatCents(1234)).toBe("12.34");
    expect(formatCents(-5)).toBe("-0.05");
  });

  it("merges quantities by sku", () => {
    const pen = { sku: "p1", name: "pen", priceCents: 150, quantity: 1 };
    expect(addLine([pen], pen)[0].quantity).toBe(2);
  });
});
