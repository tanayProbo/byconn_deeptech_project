# Extraction eval: qwen3-coder-30b-local:latest

Run 2026-09-27T09:13:00+00:00 · provider `openai` · endpoint local · max input tokens 1800 · max windows 4

| Metric | Value |
| --- | --- |
| Fixtures | 20 |
| Field precision | 99.7% |
| Field recall | 99.5% |
| Field F1 (micro) | 99.6% |
| Field F1 (macro) | 99.0% |
| Invented records | 0 |
| Schema-valid replies | 100.0% |
| Values backed by a verified quote | 100.0% |
| Latency p50 / p95 | 21.22s / 71.17s |
| Estimated input tokens | 20,153 |
| Fixtures that errored | 0 |

| Fixture | P | R | F1 | Records gold/pred/invented | Verified/unverified | Latency |
| --- | --- | --- | --- | --- | --- | --- |
| books-history | 1.00 | 1.00 | 1.00 | 18/18/0 | 36/0 | 76.26s |
| books-humor | 1.00 | 1.00 | 1.00 | 10/10/0 | 20/0 | 41.88s |
| books-mystery | 1.00 | 1.00 | 1.00 | 20/20/0 | 40/0 | 71.17s |
| books-poetry | 1.00 | 1.00 | 1.00 | 19/19/0 | 38/0 | 64.99s |
| books-science | 1.00 | 1.00 | 1.00 | 14/14/0 | 28/0 | 54.82s |
| books-travel | 1.00 | 1.00 | 1.00 | 11/11/0 | 22/0 | 41.4s |
| quotes-page-1 | 1.00 | 1.00 | 1.00 | 10/10/0 | 20/0 | 24.0s |
| quotes-page-2 | 1.00 | 1.00 | 1.00 | 10/10/0 | 20/0 | 46.89s |
| quotes-page-3 | 1.00 | 1.00 | 1.00 | 10/10/0 | 20/0 | 27.96s |
| quotes-page-4 | 1.00 | 1.00 | 1.00 | 10/10/0 | 20/0 | 20.78s |
| quotes-page-5 | 1.00 | 1.00 | 1.00 | 10/10/0 | 20/0 | 21.22s |
| quotes-page-6 | 1.00 | 1.00 | 1.00 | 10/10/0 | 20/0 | 25.89s |
| synthetic-changelog | 1.00 | 1.00 | 1.00 | 5/5/0 | 10/0 | 11.76s |
| synthetic-events | 1.00 | 1.00 | 1.00 | 4/4/0 | 8/0 | 6.61s |
| synthetic-jobs | 1.00 | 1.00 | 1.00 | 4/4/0 | 8/0 | 5.63s |
| synthetic-laptops | 1.00 | 1.00 | 1.00 | 5/5/0 | 10/0 | 6.91s |
| synthetic-menu | 1.00 | 1.00 | 1.00 | 6/6/0 | 12/0 | 9.53s |
| synthetic-pricing | 0.86 | 0.75 | 0.80 | 4/4/0 | 7/0 | 4.28s |
| synthetic-speakers | 1.00 | 1.00 | 1.00 | 5/5/0 | 10/0 | 7.37s |
| synthetic-team | 1.00 | 1.00 | 1.00 | 5/5/0 | 10/0 | 7.02s |
