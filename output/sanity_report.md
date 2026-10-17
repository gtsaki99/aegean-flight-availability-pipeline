# Task 2 sanity report

Checks on `flights_availability` (minutes; one row per airline and local day).

- Rows: 4,927 (365 days, 14 airlines)

## Invariants

| Rule | Rows breaking it |
|---|---|
| night_availability ≤ availability | 0 |
| availability ≤ n_aircraft × 25 h | 0 |
| idle_day_availability ≤ n_aircraft | 0 |
| n_aircraft > 0 | 0 |

## Rows per airline

| Airline | Days | First | Last |
|---|---|---|---|
| AA | 365 | 2015-01-01 | 2015-12-31 |
| AS | 365 | 2015-01-01 | 2015-12-31 |
| B6 | 365 | 2015-01-01 | 2015-12-31 |
| DL | 365 | 2015-01-01 | 2015-12-31 |
| EV | 365 | 2015-01-01 | 2015-12-31 |
| F9 | 365 | 2015-01-01 | 2015-12-31 |
| HA | 365 | 2015-01-01 | 2015-12-31 |
| MQ | 365 | 2015-01-01 | 2015-12-31 |
| NK | 365 | 2015-01-01 | 2015-12-31 |
| OO | 365 | 2015-01-01 | 2015-12-31 |
| UA | 365 | 2015-01-01 | 2015-12-31 |
| US | 182 | 2015-01-01 | 2015-07-01 |
| VX | 365 | 2015-01-01 | 2015-12-31 |
| WN | 365 | 2015-01-01 | 2015-12-31 |

US stops after 2015-06-30 (reported under AA from 2015-07-01). Its row on 2015-07-01 is ground time between US flights scheduled on Jun 30 but delayed past midnight.

## Per airline (2015 averages)

| Airline | Aircraft per day | Ground h per aircraft-day | Night ground h (of 8) | Idle aircraft per day | % aircraft-days idle |
|---|---|---|---|---|---|
| F9 | 56 | 12.5 | 5.8 | 2.7 | 4.9 |
| NK | 72 | 12.6 | 5.8 | 3.1 | 4.2 |
| VX | 54 | 13.2 | 6.7 | 1.8 | 3.3 |
| WN | 668 | 13.6 | 7.2 | 39.3 | 5.9 |
| AS | 139 | 13.9 | 6.7 | 10.3 | 7.4 |
| B6 | 208 | 14.3 | 6.3 | 16.8 | 8.1 |
| US | 331 | 15.8 | 6.9 | 43.9 | 13.3 |
| OO | 343 | 16.2 | 7.5 | 35.4 | 10.3 |
| DL | 738 | 16.4 | 7.2 | 134.1 | 18.2 |
| HA | 47 | 16.4 | 7.1 | 7.6 | 16.1 |
| EV | 335 | 16.6 | 7.6 | 29 | 8.7 |
| MQ | 162 | 16.6 | 7.5 | 14.1 | 8.7 |
| AA | 769 | 16.9 | 7.2 | 167.9 | 21.8 |
| UA | 649 | 17.2 | 7 | 171.9 | 26.5 |

Low-cost airlines have the least ground time; network airlines the most, and the most idle aircraft-days, which fits the documented limitation (their international flights are not in the dataset).

## Key days (all airlines)

| Day | Why | Aircraft | Ground h | Night h | Idle aircraft | Cancelled |
|---|---|---|---|---|---|---|
| 2015-01-27 | winter storm (19% cancelled) | 4,338 | 78,722 | 32,683 | 1,184 | 2,884 |
| 2015-03-07 | day before DST | 4,428 | 73,219 | 31,752 | 752 | 117 |
| 2015-03-08 | DST start: 23 h day, 7 h night | 4,431 | 66,023 | 27,830 | 676 | 122 |
| 2015-03-09 | day after DST | 4,430 | 68,407 | 31,264 | 635 | 145 |
| 2015-06-30 | last US day | 4,459 | 68,780 | 30,762 | 660 | 228 |
| 2015-07-01 | US reported under AA | 4,477 | 68,391 | 30,744 | 634 | 217 |
| 2015-11-01 | DST end: 25 h day, 9 h night | 4,409 | 75,054 | 36,455 | 679 | 86 |
| 2015-11-26 | Thanksgiving | 4,395 | 81,701 | 31,895 | 880 | 13 |

Typical day: 660 idle aircraft (median over 2015).

## Edge of the data

| Day | Aircraft counted | % of typical | Idle aircraft |
|---|---|---|---|
| 2015-01-01 | 3,732 | 84 | 3 |
| 2015-01-07 | 4,189 | 94 | 469 |
| 2015-01-14 | 4,269 | 96 | 571 |
| 2015-12-18 | 4,291 | 97 | 447 |
| 2015-12-24 | 4,223 | 95 | 445 |
| 2015-12-31 | 3,757 | 85 | 3 |

An aircraft only counts from its first to its last recorded flight (rule C), so the first and last ~3 weeks of 2015 undercount the fleet (by up to 16% on Jan 1 and Dec 31). In the daily pipeline the start effect doesn't exist (history is loaded); the end effect is why recent days are provisional.

## Estimated flying time

- Flying minutes that are estimated (diverted without arrival, cancelled after take-off): 452,650 (0.030% of total ground time)

