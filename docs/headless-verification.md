# Headless verification

Meeting: 2026-10-12 京都 4回5日.

HTML is the current client (POST for accessD and accessO, GET for accessS),
with the page cache. Headless is one Chromium process, the browser's own
user agent, and the same cname the card prints. Embedded JSON scripts were
counted. Race fields were read from the rendered DOM because those scripts
did not carry the card, odds, result, or payouts. Delays are at least 1.5s
between headless navigations. A cache hit does not wait. 403, login, and
captcha stop the run.

The production odds parser accepts a price when the page says 確定オッズ or 最終オッズ.
Finished JRA odds pages say 最終オッズ. `parser_confirmed` is that gate.
`printed_win` is the 単勝 column on that page, parsed with `read_price_cell`
(float, or null when the cell is 取消 or ----). Both sides use the same
parser, so a label gap shows up as confirmed=false on both, not as a
made-up price.

The first `doAction` for オッズ on a live card is the odds menu. This
comparison follows the race button whose cname contains that race's date
and `151ou`. The checksum is the one printed on the button.

## Timing

- HTML per race: median 5.20s, max 6.56s.
- Headless per race: median 3.68s, max 6.01s.
- HTML races that were not fully cached: median 5.20s, max 6.56s (n=11).

Per-race time is wall clock for the card, the odds page, and the result
page, including the polite wait on a cache miss or a headless navigation.
Track is once per method and is not added to each race.

## Mismatches

Mismatch count: 0.

| race_id | current seconds | headless seconds | mismatches |
|---|---:|---:|---:|
| 202608040501 | 0.00 | 6.01 | 0 |
| 202608040502 | 5.01 | 3.72 | 0 |
| 202608040503 | 5.19 | 3.32 | 0 |
| 202608040504 | 5.64 | 3.64 | 0 |
| 202608040505 | 5.11 | 3.39 | 0 |
| 202608040506 | 5.21 | 3.62 | 0 |
| 202608040507 | 5.20 | 5.18 | 0 |
| 202608040508 | 4.09 | 5.17 | 0 |
| 202608040509 | 6.56 | 5.15 | 0 |
| 202608040510 | 5.23 | 3.56 | 0 |
| 202608040511 | 5.00 | 3.27 | 0 |
| 202608040512 | 5.69 | 3.86 | 0 |
| track | 0.87 | 7.05 | 0 |

## Memory

| race_id | current RSS KiB | headless RSS KiB | current traced KiB | headless traced KiB |
|---|---:|---:|---:|---:|
| 202608040501 | 487616 | 568752 | 897 | 1385 |
| 202608040502 | 566520 | 584288 | 4435 | 931 |
| 202608040503 | 572432 | 587632 | 4536 | 926 |
| 202608040504 | 573244 | 586616 | 3575 | 909 |
| 202608040505 | 573364 | 587068 | 2963 | 745 |
| 202608040506 | 573496 | 590728 | 3165 | 773 |
| 202608040507 | 576824 | 590932 | 7735 | 1327 |
| 202608040508 | 578288 | 578268 | 5709 | 369 |
| 202608040509 | 577992 | 594956 | 6432 | 1098 |
| 202608040510 | 581044 | 594656 | 5774 | 986 |
| 202608040511 | 584080 | 598432 | 8798 | 1402 |
| 202608040512 | 586116 | 600764 | 7675 | 1309 |
| track | 593480 | 610172 | 17919 | 596 |

Race 202608040508 headless RSS is from the failed 0.25s pass. The corrected 5.17s fetch was not sampled.

## JSON payloads

Embedded JSON payload count across both methods: 0. A payload is a script whose type contains json, or whose body is a JSON array or object.

## Decision

Phase 2 proceeds. After the navigation fix, all 12 races and the track row match on values and types. Headless median 3.68s and max 6.01s cover the card, the odds page, and the result page, including the 1.5s gap. That is practical for a race day.

## Notes

- 202608040501: HTML cache hits 3/3. The HTML time is a cache read. The other 11 HTML times are live.
- 202608040508: the first headless pass resolved on the form document (0.25s, 12 mismatches, warning that the odds cname was missing). The wait now requires the access URL. A re-fetch with HTML served from cache matched: 11 entries, 11 results, 6 payouts, 11 printed win prices, 0 mismatches, headless 5.17s. The headless RSS on this row is from the failed pass and was not sampled again.
- This run recorded `parser_confirmed` false because the page says 最終オッズ and the parser then required 確定オッズ. The parser now treats 最終オッズ as final, the same gate as 確定オッズ. Printed 単勝 prices matched.
- Embedded JSON payload count is 0. Headless values are the rendered DOM run through the same parsers.
- The cushion number is not in the baba tab. Both methods follow `_data_cushion.html` named by the page script. Kyoto cushion matched at 10.7.
- Track is one row. It is not added to each race's seconds.
