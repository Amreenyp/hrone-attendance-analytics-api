# REVIEW.md

I reviewed the starter implementation against the API contract, business rules, and relevant boundary cases. The following table records the main defects identified and the corresponding changes made in `app/main.py`.

| # | Area | Starter defect | Fix implemented |
|---|---|---|---|
| 1 | Missing endpoints | Several required attendance, analytics, and explain endpoints were left as TODOs. | Implemented punch-out, attendance correction, monthly analytics, department summary, late leaderboard, department trend, and explain functionality. |
| 2 | Late-minute calculation | The grace period was evaluated using whole elapsed minutes, which mishandles boundary cases. | Compare elapsed seconds against 600 seconds, then calculate whole late minutes. |
| 3 | Overnight shifts | Shift calculations did not consistently account for shifts crossing midnight. | Added overnight-aware attendance-date, shift-start, and shift-end calculations. |
| 4 | Work hours and overtime | Work-hour rounding did not guarantee decimal half-up rounding, and overtime did not consistently enforce the 30-minute minimum. | Calculate durations and apply the required rounding and overtime threshold. |
| 5 | Health check | The original health endpoint returned success without checking MongoDB connectivity. | Ping MongoDB and return an unavailable response when the database check fails. |
| 6 | Employee creation | A check-then-insert sequence alone cannot prevent concurrent duplicate employee codes. | Declare a unique employee-code index and translate duplicate-key errors into HTTP 409. |
| 7 | Employee listing | Pagination used an incorrect offset, and the total count did not consistently reflect the active filters. | Apply the correct page offset, filtered count, and employee-code sorting. |
| 8 | Punch-in | Unknown employees, timestamps, attendance dates, and duplicate requests were not handled fully according to the contract. | Validate employee existence and status, normalize timestamps, calculate the attendance date, and rely on the unique employee/date index for duplicate protection. |
| 9 | Attendance listing | The starter loaded matching records into Python before sorting and paginating them. | Apply filtering, counting, sorting, skipping, and limiting in MongoDB. |
| 10 | Analytics calculations | The required employee and department analytics were missing from the starter. | Added monthly calculations, department summaries, competition ranking, daily trend generation, and moving averages. |
| 11 | Decimal rounding | Standard Python rounding and MongoDB `$round` do not guarantee the required half-up behavior at tie values. | Use `Decimal` with `ROUND_HALF_UP` for affected reported values. |
| 12 | Attendance corrections | The starter did not implement correction validation or an audit trail. | Recalculate derived fields, record changed fields in history, reject no-op corrections, and use conditional updates to detect conflicting changes. |

## Index considerations

A unique index on `employees.emp_code` enforces employee-code uniqueness. A unique compound index on `attendance_logs.emp_code` and `attendance_logs.date` enforces one attendance record per employee per date. Additional indexes support department/joining-date queries and attendance date filtering and sorting.

A separate non-unique index on `employees.emp_code` is redundant when the unique index already uses that field as its leading key.

## Verification notes

The Python syntax check passed, and the index-creation function executed successfully. The health endpoint returned HTTP 200. Employee listing and attendance listing returned the expected pagination and sorting structure. Duplicate employee creation returned HTTP 409, while punch-in and punch-out requests for an unknown employee returned HTTP 404.

The monthly analytics, department summary, late leaderboard, and department trend endpoints returned the expected results for the tested July 2026 data. The department trend returned five daily rows with the expected moving averages. The attendance-list explain endpoint used the `ix_attendance_date_emp` index with `IXSCAN`, examining nine keys and nine documents for the tested date range. The explicitly null `punch_in` correction test returned HTTP 422 as expected. Boundary tests for late-minute calculation also matched the expected results.

These checks cover specific cases; they do not establish that every contract rule, concurrency scenario, or large-scale performance condition has been tested.
