export const reservations = Object.freeze([
  Object.freeze({ id: "late", resource: " ROOM-A ", state: "open", startsAt: "2026-08-15T12:00:00Z" }),
  Object.freeze({ id: "early", resource: "room-b", state: "open", startsAt: "2026-08-15T08:00:00Z" }),
  Object.freeze({ id: "cancelled", resource: "room-a", state: "cancelled", startsAt: "2026-08-15T09:00:00Z" }),
  Object.freeze({ id: "adjacent", resource: "room-ab", state: "open", startsAt: "2026-08-15T10:00:00Z" }),
]);

export async function loadList() {
  const variant = process.env.RESERVATION_VARIANT ?? "after";
  if (!["before", "after"].includes(variant)) throw new Error("Use RESERVATION_VARIANT=before or after");
  const { listReservations } = await import(`../${variant}/reservations.mjs`);
  return options => listReservations(reservations, options).map(row => row.id);
}
