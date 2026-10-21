import type { CartLine } from "./cart";

/** Fetch the cart of `userId` from the inventory API. */
export async function fetchCart(baseUrl: string, userId: string): Promise<CartLine[]> {
  const response = await fetch(`${baseUrl}/carts/${encodeURIComponent(userId)}`);
  if (!response.ok) {
    throw new Error(`cart ${userId}: HTTP ${response.status}`);
  }
  return (await response.json()) as CartLine[];
}

/** Render the cart's item names into `list`, one <li> each. */
export function renderNames(list: HTMLElement, lines: readonly CartLine[]): void {
  list.replaceChildren(
    ...lines.map((line) => {
      const item = document.createElement("li");
      item.textContent = line.name;
      return item;
    }),
  );
}
