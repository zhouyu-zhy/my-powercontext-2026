export function listReservations(reservations, { state, resource } = {}) {
  const resourceCode = resource?.trim().toLowerCase();
  return reservations
    .filter(reservation =>
      (state === undefined || reservation.state === state) &&
      (!resourceCode || reservation.resource?.trim().toLowerCase() === resourceCode)
    )
    .sort((left, right) => left.startsAt.localeCompare(right.startsAt));
}
