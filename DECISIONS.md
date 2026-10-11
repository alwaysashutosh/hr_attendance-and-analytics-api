# DECISIONS.md

1. **Indexes.**
On `employees`: unique `{"emp_code": 1}` guarantees business key uniqueness; `{"department": 1, "joined_on": 1}` optimizes headcount lookups. On `attendance_logs`: unique `{"emp_code": 1, "date": 1}` guarantees one record per employee-day and resolves concurrency races; `{"date": -1, "emp_code": 1}` backs paginated attendance listing; `{"emp_code": 1, "punch_in": -1}` accelerates punch-out lookups; `{"date": 1, "late_minutes": 1}` optimizes monthly late leaderboards. I rejected an index on `{"date": 1, "punch_out": 1}` because punch-outs are addressed by `emp_code` rather than standalone timestamp scans.

2. **Punch-in race.**
Both concurrent requests compute the R1 attendance date. Instead of check-then-insert, both issue an insert against the compound unique index `{"emp_code": 1, "date": 1}`. The first insert succeeds and yields 201. The second triggers MongoDB's `DuplicateKeyError`, which our code intercepts and translates into HTTP 409.

3. **Ties.**
The pipeline uses MongoDB's `$rank` window function for standard competition ranking (1, 2, 2, 4), ordered by `total_late_minutes DESC, emp_code ASC`. Because the filter `rank <= limit` applies after ranking, all tied employees at the boundary are returned, intentionally permitting total items to exceed `limit`.

4. **Headcount.**
The pipeline begins on `employees`, matching `joined_on <= month_end` and grouping by department to establish baseline headcount before performing a `$lookup` on `attendance_logs`. Unmatched employees retain empty logs without altering headcount.

5. **One thing you would change** if this had to serve 100x the data:
Shard `attendance_logs` on `(emp_code, date)` and maintain pre-aggregated monthly summaries using change streams to eliminate recurring month-wide scans.
