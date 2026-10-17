# Profiling report

Findings from the analysis of the 2015 flights data, recomputed from the warehouse (`python -m aegean profile`). Summarised in section 2 of the write-up.


## 1. Overview

| Rows | Days | Airlines | Aircraft (tails) | First day | Last day |
|---|---|---|---|---|---|
| 5,819,079 | 365 | 14 | 4,897 | 2015-01-01 | 2015-12-31 |


## 2. Row key

The given key is (YEAR, MONTH, DAY, AIRLINE, FLIGHT_NUMBER, TAIL_NUMBER, SCHEDULED_DEPARTURE).

| Day | Airline | Flight | Tail | Route | Sched. dep | Dep | Status |
|---|---|---|---|---|---|---|---|
| 2015-08-29 | AA | 803 | (none) | STT → CLT | 1435 |  | cancelled |
| 2015-08-29 | AA | 803 | (none) | STT → SJU | 1435 |  | cancelled |
| 2015-10-16 | EV | 5,660 | N14204 | CLL → IAH | 0704 | 0934 | normal |
| 2015-10-16 | EV | 5,660 | N14204 | SHV → IAH | 0704 | 0700 | diverted |

Duplicate groups under the given key (2 groups, 4 rows). With ORIGIN_AIRPORT and DESTINATION_AIRPORT added:

| Rows sharing an extended key |
|---|
| 0 |


## 3. October airport codes

| Month | Flights | Numeric origin IDs |
|---|---|---|
| 9 | 464,946 | 0 |
| 10 | 486,165 | 486,165 |
| 11 | 467,972 | 0 |

| Mapping source | IDs |
|---|---|
| bts | 306 |
| data_vote | 1 |

Full validation (one-to-one, agreement with the flight data, route coverage): `output/lookups_report.md`.


## 4. Clock values

HHMM text as delivered; midnight is written 2400, never 0000.

| Column | Values | 2400 | 0000 | Min | Max | Distinct |
|---|---|---|---|---|---|---|
| SCHEDULED_DEPARTURE | 5,819,079 | 0 | 0 | 0001 | 2359 | 1,321 |
| DEPARTURE_TIME | 5,732,926 | 513 | 0 | 0001 | 2400 | 1,440 |
| WHEELS_OFF | 5,730,032 | 727 | 0 | 0001 | 2400 | 1,440 |
| WHEELS_ON | 5,726,566 | 2,005 | 0 | 0001 | 2400 | 1,440 |
| SCHEDULED_ARRIVAL | 5,819,079 | 2 | 0 | 0001 | 2400 | 1,435 |
| ARRIVAL_TIME | 5,726,566 | 2,456 | 0 | 0001 | 2400 | 1,440 |

| Max delay (min) | Min delay | Depart on the next day | Depart on the previous day | Arrive on the next day (origin clock) | Arrive 2 days later |
|---|---|---|---|---|---|
| 1,988 | -82 | 17,537 | 190 | 269,999 | 6 |


## 5. Daily volume

| Min | P5 | Median | Mean | P95 | Max | Std dev |
|---|---|---|---|---|---|---|
| 10,068 | 12,736 | 16,379 | 15,943 | 17,417 | 17,574 | 1,374 |

| Weekday | Avg flights | Min | Max |
|---|---|---|---|
| Monday | 16,645 | 14,808 | 17,453 |
| Tuesday | 16,242 | 15,155 | 17,308 |
| Wednesday | 16,460 | 15,383 | 17,342 |
| Thursday | 16,463 | 10,068 | 17,467 |
| Friday | 16,581 | 12,633 | 17,517 |
| Saturday | 13,472 | 11,708 | 16,072 |
| Sunday | 15,726 | 12,627 | 17,574 |

| Lowest days | Weekday | Flights | Cancelled |
|---|---|---|---|
| 2015-11-26 | Thursday | 10,068 | 13 |
| 2015-10-31 | Saturday | 11,708 | 178 |
| 2015-09-05 | Saturday | 11,986 | 35 |
| 2015-01-31 | Saturday | 12,037 | 130 |
| 2015-01-17 | Saturday | 12,070 | 32 |
| 2015-12-05 | Saturday | 12,200 | 62 |

CSV size 592,406,591 bytes / 5,819,079 rows = **101.8 bytes per row**.

| n (days per feed) | Max rows | Max CSV MB |
|---|---|---|
| 7 | 118,837 | 12.1 |
| 8 | 136,290 | 13.9 |
| 10 | 170,995 | 17.4 |
| 14 | 237,340 | 24.2 |


## 6. Timezones

| Timezone source | Airports | Zones |
|---|---|---|
| coordinates | 319 | 21 |
| manual | 3 | 2 |

| Normal flights | Crossing a zone |
|---|---|
| 5,714,008 | 3,128,187 |

| Timezone rule | Flights | On DST weekends |
|---|---|---|
| warn_air_time_tz | 78 | 61 |
| warn_elapsed_vs_arrival | 79 | 62 |
| warn_scheduled_time_tz | 99 | 41 |

| Origin zone | Flights | % on another UTC date |
|---|---|---|
| America/New_York | 2,054,833 | 12.1 |
| America/Chicago | 1,721,917 | 17 |
| America/Los_Angeles | 1,064,973 | 29.2 |
| America/Denver | 410,416 | 23.9 |
| America/Phoenix | 180,036 | 26.4 |
| America/Detroit | 148,747 | 14.3 |

All flights: 18.8% depart on a different UTC date than their flight day.


## 7. Time-consistency rules

Row flags from staging (strict rules block a feed above 0.01% of rows; warn rules alert).

| Rule | Severity | Rows flagged |
|---|---|---|
| cast_failure | strict | 0 |
| bad_date | strict | 0 |
| bad_clock | strict | 0 |
| unknown_airline | strict | 0 |
| unknown_airport | strict | 0 |
| status_both | strict | 0 |
| status_normal_missing | strict | 0 |
| status_cancelled_fields | strict | 0 |
| status_diverted_fields | strict | 0 |
| status_reason_not_cancelled | strict | 0 |
| unknown_cancellation_reason | strict | 0 |
| rule_departure_delay | strict | 1 |
| rule_elapsed_parts | strict | 0 |
| rule_wheels_off | strict | 0 |
| rule_arrival_taxi_in | strict | 0 |
| rule_arrival_delay | strict | 0 |
| rule_delay_causes | strict | 0 |
| warn_elapsed_vs_arrival | warn | 79 |
| warn_air_time_tz | warn | 78 |
| warn_scheduled_time_tz | warn | 99 |

| Day | Airline | Flight | Status | Sched. dep | Dep | Delay |
|---|---|---|---|---|---|---|
| 2015-12-24 | NK | 1,012 | cancelled | 0804 | 1103 | 1 |

The one strict-rule failure in 2015.


## 8. Cancelled and diverted flights

| Status | Flights | With tail | With dep. time | With wheels-off | With elapsed | With arrival |
|---|---|---|---|---|---|---|
| normal | 5,714,008 | 5,714,008 | 5,714,008 | 5,714,008 | 5,714,008 | 5,714,008 |
| cancelled | 89,884 | 75,163 | 3,731 | 837 | 0 | 0 |
| diverted | 15,187 | 15,187 | 15,187 | 15,187 | 0 | 12,558 |

| Reason | Cancelled | After pushback | After wheels-off |
|---|---|---|---|
| A Airline/Carrier | 25,262 | 1,298 | 290 |
| B Weather | 48,851 | 1,211 | 495 |
| C National Air System | 15,749 | 384 | 52 |
| D Security | 22 | 1 | 0 |

| Median % cancelled | Max % cancelled | Worst day | Median % diverted | Max % diverted |
|---|---|---|---|---|
| 0.71 | 19 | 2015-01-27 | 0.2 | 1.15 |

| Diverted without arrival | With a continuation row |
|---|---|
| 2,629 | 2 |

One of the two continuations is EV 5660 (SHV → IAH diverted, CLL → IAH continued); the other is cancelled.

| Cancelled after wheels-off | Next departs from origin | From destination | From elsewhere | Never again in 2015 |
|---|---|---|---|---|
| 837 | 641 | 106 | 87 | 3 |


## 9. Aircraft flying for two airlines (US → AA)

| Airlines | Aircraft | First switch | Last switch | 1 day | 2–3 days | 4–7 days | 8–30 days | 31+ days | Longest gap |
|---|---|---|---|---|---|---|---|---|---|
| AA / US | 337 | 2015-07-01 | 2015-11-20 | 282 | 22 | 7 | 9 | 17 | 192 |

| Airline | Month | Aircraft | Flights |
|---|---|---|---|
| AA | 2015-05 | 601 | 44,710 |
| US | 2015-05 | 331 | 33,761 |
| AA | 2015-06 | 605 | 44,360 |
| US | 2015-06 | 331 | 34,300 |
| AA | 2015-07 | 927 | 81,434 |
| AA | 2015-08 | 933 | 79,748 |

Carrier code US appears only until 2015-06-30; BTS: "American and US Airways ... began reporting joint data in July 2015."


## 10. Overlapping flights per aircraft (UTC)

Intervals: normal flights and diverted flights with an arrival.

| Overlaps | % of flights | Aircraft | Overlapping minutes | Next flight from another airport |
|---|---|---|---|---|
| 3,593 | 0.063 | 950 | 393,864 | 2,795 |

| Overlap | Cases | Next from another airport |
|---|---|---|
| 1–5 min | 106 | 70 |
| 6–15 min | 234 | 170 |
| 16–60 min | 958 | 658 |
| 1–3 h | 1,576 | 1,263 |
| > 3 h | 719 | 634 |

| Airline | Overlaps | Per 10k flights |
|---|---|---|
| DL | 3,017 | 34.6 |
| UA | 309 | 6.1 |
| F9 | 31 | 3.4 |
| VX | 6 | 1 |
| AS | 16 | 0.9 |

| Near 24 h (wrong date) | On DST weekends | Same flight twice | Current flight doesn't fit the chain | Next flight doesn't fit the chain |
|---|---|---|---|---|
| 0 | 32 | 7 | 2,069 | 1,189 |

Alternative explanations tested: mostly wrong tail numbers (chain test), little date/DST/duplicate effect.


## 11. Flights missing from the dataset (limitation)

| Gaps | Chain breaks | % | Median break gap (min) | Median normal gap (min) |
|---|---|---|---|---|
| 5,718,077 | 66,171 | 1.16 | 1,088 | 62 |

| Landed → next departure | Chain breaks |
|---|---|
| DFW → MIA | 1,821 |
| MIA → DFW | 1,808 |
| ORD → SFO | 1,527 |
| EWR → ORD | 1,422 |
| ORD → EWR | 1,320 |
| ORD → DEN | 1,062 |

| Airline | % chain breaks | Long daytime gaps per 1k | % of ground time in these gaps |
|---|---|---|---|
| UA | 2.45 | 49.9 | 43.9 |
| AA | 1.24 | 51.3 | 38.3 |
| DL | 1.22 | 32.1 | 34.4 |
| HA | 0.29 | 26.1 | 31.1 |
| US | 0.59 | 33.8 | 28.7 |
| B6 | 1.33 | 31.9 | 26.8 |
| EV | 1.64 | 24.3 | 26.3 |
| MQ | 0.75 | 23.5 | 25.7 |
| OO | 1.42 | 19.3 | 23.7 |
| F9 | 1.52 | 11.4 | 20.1 |
| NK | 1.31 | 20.8 | 19.1 |
| AS | 0.43 | 18.5 | 16.2 |
| VX | 0.19 | 24.1 | 13.9 |
| WN | 0.51 | 7.7 | 11.5 |

Long daytime gap: landed 06:00–17:59 local and next departure from the same airport ≥ 10 h later. Descriptive only: the cause of a single gap can't be told from the data.


## 12. Which aircraft count on a day (rule C)

| A: flown | B: scheduled | Grounded all day by cancellations | C adds: days without any flight |
|---|---|---|---|
| 1,360,662 | 1,364,829 | 4,167 | 243,084 |

| Absence | Absences | Idle days |
|---|---|---|
| 1 day | 42,273 | 42,273 |
| 2–7 days | 20,462 | 62,027 |
| 8–30 days | 4,753 | 70,093 |
| > 30 days | 1,106 | 68,691 |

| Day | Aircraft counted | % of typical | Idle aircraft |
|---|---|---|---|
| 2015-01-01 | 3,732 | 84 | 3 |
| 2015-01-07 | 4,189 | 94 | 469 |
| 2015-01-14 | 4,269 | 96 | 571 |
| 2015-06-15 | 4,450 | 100 | 627 |
| 2015-12-18 | 4,291 | 97 | 447 |
| 2015-12-24 | 4,223 | 95 | 445 |
| 2015-12-31 | 3,757 | 85 | 3 |

Data-boundary effect: an aircraft only counts from its first to its last recorded flight, so the first and last ~3 weeks of 2015 undercount the fleet (by up to 16%).

