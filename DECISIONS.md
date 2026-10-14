# DECISIONS.md

## 1. Indexes

I use a unique index on `employees.emp_code` to prevent duplicate employee codes, including during concurrent requests. A compound index on `department` and `joined_on` supports department and joining-date queries. On `attendance_logs`, a unique compound index on `emp_code` and `date` enforces one record per employee per attendance date. An additional index on `date` descending and `emp_code` ascending supports attendance filtering and sorting. A separate non-unique employee-code index is unnecessary because the unique index already supports lookups by that field.

## 2. Punch-in race handling

Two concurrent requests may both check that no attendance record exists. The unique compound index allows only one insert for an employee and date. The losing insert raises a duplicate-key error, which the API converts to HTTP 409 Conflict.

## 3. Leaderboard ties

The leaderboard uses competition ranking: tied totals receive the same rank, and the next rank skips accordingly. If a tie crosses the requested limit, all employees with that rank are returned. Employee code provides deterministic ordering among tied results.

## 4. Department headcount

The department summary starts with employees who joined by the end of the requested month and then looks up their attendance logs. This includes employees with no attendance records in the month.

## 5. Scaling to 100 times the data

I would evaluate precomputed daily aggregates for frequently requested analytics, while retaining raw attendance records for corrections and auditing. I would measure query plans and workload patterns before adding further indexes or introducing caching.

## 6. Concurrent corrections

Corrections use conditional updates based on the record's original status and punch timestamps. If another request changes those values first, the update fails and returns HTTP 409. This prevents a stale correction from silently overwriting the newer record and its audit history.