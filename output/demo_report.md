# Daily pipeline demo

Initial load up to 2015-06-30; daily runs 2015-07-01 … 2015-08-04 (n = 10).

## Runs

| Day | Delivered | Outcome | Alerts |
|---|---|---|---|
| 2015-07-01 | daily 2015-07-01 → LANDED | daily 2015-07-01: PROCESSED | airline_disappeared; tail_airline_change |
| 2015-07-02 | daily 2015-07-02 → LANDED | daily 2015-07-02: PROCESSED |  |
| 2015-07-03 | daily 2015-07-03 → LANDED (changes final day 2015-06-25) | daily 2015-07-03: PROCESSED | reconciliation |
| 2015-07-04 | daily 2015-07-04 → LANDED | daily 2015-07-04: PROCESSED |  |
| 2015-07-05 | daily 2015-07-05 → LANDED | daily 2015-07-05: PROCESSED |  |
| 2015-07-06 | daily 2015-07-06 → LANDED (truncated) | daily 2015-07-06: QUARANTINED | coverage |
| 2015-07-07 | daily 2015-07-07 → LANDED | daily 2015-07-07: PROCESSED |  |
| 2015-07-08 | daily 2015-07-08 → LANDED (extra column) | daily 2015-07-08: QUARANTINED | schema |
| 2015-07-09 | daily 2015-07-09 → LANDED<br>daily 2015-07-07 → DUPLICATE (2015-07-07 file sent again) | daily 2015-07-09: PROCESSED |  |
| 2015-07-10 | daily 2015-07-10 → LANDED | daily 2015-07-10: PROCESSED |  |
| 2015-07-11 | nothing (provider down) | nothing to process |  |
| 2015-07-12 | nothing (provider down) | nothing to process |  |
| 2015-07-13 | daily 2015-07-13 → LANDED | daily 2015-07-13: PROCESSED |  |
| 2015-07-14 | daily 2015-07-14 → LANDED | daily 2015-07-14: PROCESSED |  |
| 2015-07-15 | daily 2015-07-15 → LANDED<br>daily 2015-07-03 → LANDED (old feed sent again, reordered) | daily 2015-07-03: SKIPPED_OLD; daily 2015-07-15: PROCESSED |  |
| 2015-07-16 | daily 2015-07-16 → LANDED | daily 2015-07-16: PROCESSED |  |
| 2015-07-17 | daily 2015-07-17 → LANDED | daily 2015-07-17: PROCESSED |  |
| 2015-07-18 | daily 2015-07-18 → LANDED | daily 2015-07-18: PROCESSED |  |
| 2015-07-19 | daily 2015-07-19 → LANDED | daily 2015-07-19: PROCESSED |  |
| 2015-07-20 | nothing (provider down) | nothing to process |  |
| 2015-07-21 | nothing (provider down) | nothing to process |  |
| 2015-07-22 | nothing (provider down) | nothing to process |  |
| 2015-07-23 | nothing (provider down) | nothing to process |  |
| 2015-07-24 | nothing (provider down) | nothing to process |  |
| 2015-07-25 | daily 2015-07-25 → LANDED | daily 2015-07-25: PROCESSED | coverage_gap |
| 2015-07-26 | daily 2015-07-26 → LANDED<br>backfill 2015-07-26 → LANDED (backfill for the gap) | daily 2015-07-26: PROCESSED; backfill 2015-07-26: PROCESSED |  |
| 2015-07-27 | daily 2015-07-27 → LANDED | processing outage: not run |  |
| 2015-07-28 | daily 2015-07-28 → LANDED | processing outage: not run |  |
| 2015-07-29 | daily 2015-07-29 → LANDED | daily 2015-07-27: PROCESSED; daily 2015-07-28: PROCESSED; daily 2015-07-29: PROCESSED |  |
| 2015-07-30 | daily 2015-07-30 → LANDED | daily 2015-07-30: PROCESSED |  |
| 2015-07-31 | daily 2015-07-31 → LANDED | daily 2015-07-31: PROCESSED |  |
| 2015-08-01 | daily 2015-08-01 → LANDED | daily 2015-08-01: PROCESSED |  |
| 2015-08-02 | daily 2015-08-02 → LANDED | daily 2015-08-02: PROCESSED |  |
| 2015-08-03 | daily 2015-08-03 → LANDED | daily 2015-08-03: PROCESSED |  |
| 2015-08-04 | daily 2015-08-04 → LANDED | daily 2015-08-04: PROCESSED |  |

## Scenario checks

| Scenario | Expected | Result |
|---|---|---|
| US → AA switch detected on 2015-07-01 | airline disappeared + aircraft moves | ✓ |
| Final day changed (feed 2015-07-03) | reconciliation alert | ✓ |
| Truncated feed 2015-07-06 | QUARANTINED | ✓ |
| Schema change 2015-07-08 | QUARANTINED | ✓ |
| 2015-07-07 file sent twice | DUPLICATE | ✓ |
| Old feed sent again on 2015-07-15 | SKIPPED_OLD | ✓ |
| 2 missed feeds (weekend) | no coverage gap on 2015-07-13 | ✓ |
| 5 missed feeds | coverage gap on 2015-07-25 | ✓ |
| Backfill 2015-07-26 | PROCESSED, no unverified days left | ✓ |
| Processing outage 2015-07-27..28 | caught up, watermark = last day | ✓ |
| Final days 2015-06-21 … 2015-07-29 equal the original file | 39 of 39 days | ✓ |
| Rebuild from the archive | same flights (all days) and same flights_availability | ✓ |

Final days compared with the original file: 39/39 identical. Days after 2015-07-29 are still inside the correction window of the last feed.
Rebuild: 216/216 day digests identical; flights_availability rows differing: 0.
Total run time: 616s.
