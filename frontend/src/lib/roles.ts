/* Roles (CLAUDE.md §8). A floor, never an exact match: attendant < manager < admin, mirroring
 * app/core/roles.py::satisfies. Role gates *rendering* only -- §8: "hiding a button is UX, not
 * a control" -- so every screen still handles a real 403. */

export type Role = "attendant" | "manager" | "admin";

const RANK: Record<string, number> = { attendant: 0, manager: 1, admin: 2 };

export function satisfies(held: string | null | undefined, minimum: Role): boolean {
  return (RANK[held ?? ""] ?? -1) >= (RANK[minimum] ?? 99);
}
