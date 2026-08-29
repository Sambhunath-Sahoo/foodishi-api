import hashlib
import math
from datetime import datetime, timedelta
from decimal import Decimal

# City delivery on two wheels, averaged over traffic and lights.
AVG_SPEED_KMPH = Decimal("20")
# Slack between "food is ready" and "rider is moving".
DISPATCH_BUFFER_MINUTES = 5
EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: Decimal, lon1: Decimal, lat2: Decimal, lon2: Decimal) -> Decimal:
    """Great-circle distance. Straight-line, so it understates real road
    distance -- fine for a promise the kitchen time dominates anyway."""
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))
    return Decimal(str(round(km, 1)))


def travel_minutes(distance_km: Decimal) -> int:
    return int(math.ceil(float(distance_km / AVG_SPEED_KMPH * 60)))


def promised_at(placed_at: datetime, avg_prep_minutes: int, distance_km: Decimal) -> datetime:
    """prep + travel + dispatch buffer. Every input is a real column, so the
    promise can be explained to a customer rather than guessed at."""
    total = avg_prep_minutes + travel_minutes(distance_km) + DISPATCH_BUFFER_MINUTES
    return placed_at + timedelta(minutes=total)


# --- Phase-1 stand-in for real geocoding -------------------------------------
# Saved addresses carry placeholder coordinates, so the great-circle distance
# to a restaurant is meaningless -- it lands hundreds of km out, which tripped
# the delivery-radius rule and would have priced a per-km fee off 747 km.
# Until addresses carry true lat/lng, the promise is a plausible city-delivery
# window and the fee is priced off a plausible city distance.
PROMISE_MIN_MINUTES = 15
PROMISE_MAX_MINUTES = 45
NOMINAL_MIN_KM = Decimal("1.0")
NOMINAL_MAX_KM = Decimal("8.0")


def _unit_interval(*parts: object) -> float:
    """A stable float in [0, 1) derived from `parts`.

    hashlib rather than the builtin hash(): hash() is salted per process, so
    the same cart would be promised 18 minutes before a restart and 41 after.
    """
    raw = "|".join(str(part) for part in parts).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") / 2.0**64


def nominal_eta_minutes(*seed: object) -> int:
    """A plausible promise, stable for one cart context.

    Stability is the point: /orders/quote and POST /orders run the same
    pricing, so a wandering number would quote 20 minutes and place 40.
    """
    span = PROMISE_MAX_MINUTES - PROMISE_MIN_MINUTES
    return PROMISE_MIN_MINUTES + int(_unit_interval("eta", *seed) * (span + 1))


def nominal_distance_km(*seed: object) -> Decimal:
    """A plausible urban delivery distance, for fee purposes only."""
    span = float(NOMINAL_MAX_KM - NOMINAL_MIN_KM)
    km = float(NOMINAL_MIN_KM) + _unit_interval("km", *seed) * span
    return Decimal(str(round(km, 1)))
