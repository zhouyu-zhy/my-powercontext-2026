export function listReservations(reservations, { state } = {}) {
  return reservations
    .filter(reservation => state === undefined || reservation.state === state)
    .sort((left, right) => left.startsAt.localeCompare(right.startsAt));
}
