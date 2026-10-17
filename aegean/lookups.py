"""Build the lookup tables.

- airports: airports.csv + IANA timezone + BTS numeric airport ID (used by October 2015,
  which has BTS IDs instead of IATA codes in ORIGIN_AIRPORT / DESTINATION_AIRPORT)
- airlines: copy of airlines.csv
- airline_groups, holidays, cancellation_reasons: hand-maintained files in data/lookups

Every step validates its result and raises LookupBuildError if a rule is broken, so a
bad lookup never reaches the pipeline.
"""
import shutil
import urllib.request
from dataclasses import dataclass, field

import duckdb
import pandas as pd
from timezonefinder import TimezoneFinder

from . import config

# TranStats serves its lookup tables under ROT13-encoded names:
# L_AIRPORT_ID -> Y_NVecbeg_VQ, L_AIRPORT -> Y_NVecbeg.
BTS_URL = "https://www.transtats.bts.gov/Download_Lookup.asp?Y11x72={}"
BTS_TABLES = {"L_AIRPORT_ID": "Y_NVecbeg_VQ", "L_AIRPORT": "Y_NVecbeg"}

# airports.csv has no coordinates for these three.
MANUAL_TZ = {"ECP": "America/Chicago", "PBG": "America/New_York", "UST": "America/New_York"}
# airports.csv has sign errors for these two (the points fall in the ocean).
COORD_FIX = {"GUM": (13.48345, 144.79598), "PPG": (-14.33102, -170.71053)}

# A data vote may only resolve an ID when the evidence is strong.
MIN_VOTES = 5
MIN_VOTE_SHARE = 0.9
MIN_ROUTE_COVERAGE = 0.98


class LookupBuildError(Exception):
    pass


@dataclass
class Report:
    lines: list = field(default_factory=list)

    def add(self, text=""):
        self.lines.append(text)

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def download_bts_lookups(refresh=False):
    """Download the two BTS lookups once and cache them (converted to UTF-8) in data/lookups/bts."""
    config.BTS_DIR.mkdir(parents=True, exist_ok=True)
    for table, encoded in BTS_TABLES.items():
        target = config.BTS_DIR / f"{table}.csv"
        if target.exists() and not refresh:
            continue
        request = urllib.request.Request(BTS_URL.format(encoded), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("latin-1")  # BTS files are Latin-1, not UTF-8
        if not body.startswith("Code,Description"):
            raise LookupBuildError(f"Unexpected content downloading {table} from BTS")
        target.write_text(body, encoding="utf-8", newline="")  # keep BTS's \r\n as is


def resolve_airport_ids(con):
    """Map every BTS airport ID used in October to an IATA code.

    Expects relations `flights` (MONTH, AIRLINE, FLIGHT_NUMBER, SCHEDULED_DEPARTURE,
    ORIGIN_AIRPORT, DESTINATION_AIRPORT), `bts_ids` / `bts_codes` (Code, Description)
    and `airports` (IATA_CODE). Returns AIRPORT_ID, IATA_CODE, DESCRIPTION, MAP_SOURCE.

    Primary source: the BTS lookups joined on their description, keeping only codes that
    exist in airports.csv. Fallback when BTS gives no valid code (e.g. a renamed airport):
    a vote over (airline, flight number, scheduled departure) signatures that also fly in
    September/November with IATA codes.
    """
    con.sql("""
        create or replace temp table oct_ids as
        select distinct id from (
            select ORIGIN_AIRPORT id from flights where MONTH = '10'
            union select DESTINATION_AIRPORT from flights where MONTH = '10')""")
    con.sql("""
        create or replace temp table bts_candidates as
        select o.id, i.Description description, c.Code iata
        from oct_ids o
        join bts_ids i on i.Code = o.id
        join bts_codes c on c.Description = i.Description
        where c.Code in (select IATA_CODE from airports)""")
    con.sql("""
        create or replace temp table vote as
        with o as (select distinct ORIGIN_AIRPORT id, AIRLINE, FLIGHT_NUMBER, SCHEDULED_DEPARTURE
                   from flights where MONTH = '10'),
             s as (select distinct ORIGIN_AIRPORT iata, AIRLINE, FLIGHT_NUMBER, SCHEDULED_DEPARTURE
                   from flights where MONTH in ('9', '11'))
        select id, iata, count(*) votes, count(*) / sum(count(*)) over (partition by id) vote_share
        from o join s using (AIRLINE, FLIGHT_NUMBER, SCHEDULED_DEPARTURE)
        group by id, iata
        qualify row_number() over (partition by id order by count(*) desc, iata) = 1""")
    return con.sql(f"""
        with b as (select id, count(*) n, any_value(description) description,
                          list(iata order by iata) codes from bts_candidates group by id)
        select o.id AIRPORT_ID,
               case when b.n = 1 then b.codes[1]
                    when b.n > 1 and list_contains(b.codes, v.iata) then v.iata
                    when b.n is null and v.votes >= {MIN_VOTES} and v.vote_share >= {MIN_VOTE_SHARE} then v.iata
               end IATA_CODE,
               coalesce(b.description, (select Description from bts_ids where Code = o.id)) DESCRIPTION,
               case when b.n = 1 then 'bts' when b.n > 1 then 'bts+data_vote' when v.iata is not null then 'data_vote' end MAP_SOURCE,
               v.iata vote_iata, v.votes, v.vote_share
        from oct_ids o left join b using (id) left join vote v using (id)
        order by AIRPORT_ID""").df()


def validate_airport_ids(mapping, report):
    unmapped = mapping[mapping.IATA_CODE.isna()]
    if len(unmapped):
        raise LookupBuildError(f"October airport IDs without a code: {unmapped.AIRPORT_ID.tolist()}")
    dupes = mapping[mapping.IATA_CODE.duplicated(keep=False)]
    if len(dupes):
        raise LookupBuildError(f"Mapping is not one-to-one: {dupes[['AIRPORT_ID', 'IATA_CODE']].values.tolist()}")
    both = mapping[mapping.vote_iata.notna()]
    disagree = both[both.IATA_CODE != both.vote_iata]
    if len(disagree):
        raise LookupBuildError(f"BTS and data vote disagree: {disagree[['AIRPORT_ID', 'IATA_CODE', 'vote_iata']].values.tolist()}")
    report.add(f"- October airport IDs: {len(mapping)}, all mapped, one-to-one")
    report.add(f"- Sources: {mapping.MAP_SOURCE.value_counts().to_dict()}")
    report.add(f"- BTS and data vote agree on {len(both)}/{len(both)} IDs where both exist")
    for r in mapping[mapping.MAP_SOURCE != "bts"].itertuples():
        report.add(f"  - {r.AIRPORT_ID} → {r.IATA_CODE} ({r.MAP_SOURCE}; {r.DESCRIPTION}; "
                   f"vote {r.votes} signatures, {r.vote_share:.1%})")


def route_coverage(con, mapping):
    """Share of October flights (after mapping) on airline + route combinations flown in September."""
    con.register("id_map", mapping[["AIRPORT_ID", "IATA_CODE"]])
    return con.sql("""
        with o as (select mo.IATA_CODE org, md.IATA_CODE dst, AIRLINE, count(*) n from flights
                   join id_map mo on mo.AIRPORT_ID = ORIGIN_AIRPORT join id_map md on md.AIRPORT_ID = DESTINATION_AIRPORT
                   where MONTH = '10' group by all),
             s as (select distinct ORIGIN_AIRPORT org, DESTINATION_AIRPORT dst, AIRLINE from flights where MONTH = '9')
        select sum(n) filter (where s.org is not null) / sum(n) from o left join s using (org, dst, AIRLINE)""").fetchone()[0]


def assign_timezones(airports):
    """Add TIMEZONE, TZ_SOURCE and COORD_FIXED to an airports.csv frame."""
    ap = airports.copy()
    ap["COORD_FIXED"] = ap.IATA_CODE.isin(COORD_FIX.keys())
    for code, (lat, lon) in COORD_FIX.items():
        ap.loc[ap.IATA_CODE == code, ["LATITUDE", "LONGITUDE"]] = [lat, lon]
    finder = TimezoneFinder()
    zones, sources = [], []
    for r in ap.itertuples():
        if r.IATA_CODE in MANUAL_TZ:
            zones.append(MANUAL_TZ[r.IATA_CODE]); sources.append("manual")
        elif pd.isna(r.LATITUDE) or pd.isna(r.LONGITUDE):
            zones.append(None); sources.append(None)
        else:
            zones.append(finder.timezone_at(lat=float(r.LATITUDE), lng=float(r.LONGITUDE))); sources.append("coordinates")
    ap["TIMEZONE"], ap["TZ_SOURCE"] = zones, sources

    missing = ap[ap.TIMEZONE.isna()]
    if len(missing):
        raise LookupBuildError(f"Airports without a timezone: {missing.IATA_CODE.tolist()}")
    # A generic Etc/ zone means the coordinates are in the ocean, i.e. wrong.
    generic = ap[ap.TIMEZONE.str.startswith("Etc/")]
    if len(generic):
        raise LookupBuildError(f"Airports with a generic ocean timezone (check coordinates): "
                               f"{generic[['IATA_CODE', 'TIMEZONE']].values.tolist()}")
    return ap


def build_lookups(refresh_bts=False):
    report = Report(["# Lookup build report", ""])
    config.LOOKUP_DIR.mkdir(parents=True, exist_ok=True)
    download_bts_lookups(refresh=refresh_bts)

    airports = pd.read_csv(config.raw_file("airports.csv"), dtype={"IATA_CODE": str})
    con = duckdb.connect()
    con.sql(f"""create view flights as
        select MONTH, AIRLINE, FLIGHT_NUMBER, SCHEDULED_DEPARTURE, ORIGIN_AIRPORT, DESTINATION_AIRPORT
        from read_csv('{config.raw_file("flights.csv").as_posix()}', all_varchar = true)""")
    con.sql(f"create view bts_ids as select * from read_csv('{(config.BTS_DIR / 'L_AIRPORT_ID.csv').as_posix()}', all_varchar = true)")
    con.sql(f"create view bts_codes as select * from read_csv('{(config.BTS_DIR / 'L_AIRPORT.csv').as_posix()}', all_varchar = true)")
    con.register("airports", airports)

    report.add("## October airport IDs")
    mapping = resolve_airport_ids(con)
    validate_airport_ids(mapping, report)
    coverage = route_coverage(con, mapping)
    report.add(f"- October flights on airline + route combinations also flown in September: {coverage:.2%}")
    if coverage < MIN_ROUTE_COVERAGE:
        raise LookupBuildError(f"Route coverage {coverage:.2%} below {MIN_ROUTE_COVERAGE:.0%}")
    mapping[["AIRPORT_ID", "IATA_CODE", "DESCRIPTION", "MAP_SOURCE"]].to_csv(
        config.LOOKUP_DIR / "airport_id_to_iata.csv", index=False)

    report.add("")
    report.add("## Timezones")
    ap = assign_timezones(airports)
    ap = ap.merge(mapping[["AIRPORT_ID", "IATA_CODE", "MAP_SOURCE"]].rename(
        columns={"AIRPORT_ID": "BTS_AIRPORT_ID", "MAP_SOURCE": "BTS_ID_SOURCE"}), on="IATA_CODE", how="left")
    report.add(f"- Airports: {len(ap)}; zone from coordinates {int((ap.TZ_SOURCE == 'coordinates').sum())}, "
               f"manual {int((ap.TZ_SOURCE == 'manual').sum())} ({', '.join(MANUAL_TZ)})")
    report.add(f"- Coordinates corrected: {', '.join(f'{k} {v}' for k, v in COORD_FIX.items())}")
    report.add(f"- Distinct zones: {ap.TIMEZONE.nunique()}; airports with a BTS ID: {int(ap.BTS_AIRPORT_ID.notna().sum())}")
    split = ap.groupby("STATE").TIMEZONE.nunique()
    report.add(f"- States spanning more than one zone: {', '.join(split[split > 1].index)}")
    ap.to_csv(config.LOOKUP_DIR / "airports.csv", index=False)

    shutil.copyfile(config.raw_file("airlines.csv"), config.LOOKUP_DIR / "airlines.csv")
    report.add("")
    report.add("## Airlines")
    report.add(f"- Copied airlines.csv ({len(pd.read_csv(config.LOOKUP_DIR / 'airlines.csv'))} airlines)")

    report.write(config.OUTPUT_DIR / "lookups_report.md")
    return report
