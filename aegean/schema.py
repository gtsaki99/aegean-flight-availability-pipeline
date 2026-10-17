"""Source columns of the flights feed, in delivery order, with their stored types.

CLOCK columns are 4-digit local HHMM values (0001–2400) and are stored as delivered (text).
"""

SOURCE_COLUMNS = [
    ("YEAR", "SMALLINT"), ("MONTH", "TINYINT"), ("DAY", "TINYINT"), ("DAY_OF_WEEK", "TINYINT"),
    ("AIRLINE", "VARCHAR"), ("FLIGHT_NUMBER", "INTEGER"), ("TAIL_NUMBER", "VARCHAR"),
    ("ORIGIN_AIRPORT", "VARCHAR"), ("DESTINATION_AIRPORT", "VARCHAR"),
    ("SCHEDULED_DEPARTURE", "CLOCK"), ("DEPARTURE_TIME", "CLOCK"), ("DEPARTURE_DELAY", "SMALLINT"),
    ("TAXI_OUT", "SMALLINT"), ("WHEELS_OFF", "CLOCK"), ("SCHEDULED_TIME", "SMALLINT"),
    ("ELAPSED_TIME", "SMALLINT"), ("AIR_TIME", "SMALLINT"), ("DISTANCE", "SMALLINT"),
    ("WHEELS_ON", "CLOCK"), ("TAXI_IN", "SMALLINT"), ("SCHEDULED_ARRIVAL", "CLOCK"),
    ("ARRIVAL_TIME", "CLOCK"), ("ARRIVAL_DELAY", "SMALLINT"),
    ("DIVERTED", "BOOLEAN"), ("CANCELLED", "BOOLEAN"), ("CANCELLATION_REASON", "VARCHAR"),
    ("AIR_SYSTEM_DELAY", "SMALLINT"), ("SECURITY_DELAY", "SMALLINT"), ("AIRLINE_DELAY", "SMALLINT"),
    ("LATE_AIRCRAFT_DELAY", "SMALLINT"), ("WEATHER_DELAY", "SMALLINT"),
]
SOURCE_NAMES = [name for name, _ in SOURCE_COLUMNS]
CLOCK_COLUMNS = [name for name, kind in SOURCE_COLUMNS if kind == "CLOCK"]
CAST_COLUMNS = [(name, kind) for name, kind in SOURCE_COLUMNS if kind not in ("VARCHAR", "CLOCK")]

# The given key plus origin and destination: unique across 2015 (the given key alone has 2 duplicate groups).
EXTENDED_KEY = ["YEAR", "MONTH", "DAY", "AIRLINE", "FLIGHT_NUMBER", "TAIL_NUMBER", "SCHEDULED_DEPARTURE",
                "ORIGIN_AIRPORT", "DESTINATION_AIRPORT"]

NULL_TOKEN = "\\N"   # canonical form of NULL inside fingerprints (concat_ws would skip NULLs)
SEPARATOR = "chr(31)"


def fingerprint(columns):
    """SQL expression: md5 of the columns' canonical text form, as 16 bytes (BLOB)."""
    parts = ", ".join(f"coalesce(cast({c} as varchar), '{NULL_TOKEN}')" for c in columns)
    return f"unhex(md5(concat_ws({SEPARATOR}, {parts})))"
