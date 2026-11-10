# Token usage and cost report

Final full-dataset run: `requests.csv` -> `output.csv` (250 requests, 1.69 s wall clock).

| Item | Value |
|---|---|
| Model providers / names | none - the shipped pipeline is fully deterministic |
| Model calls | 0 |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens per request | 0 |
| Estimated total cost (USD) | 0.00 |
| Estimated cost per request (USD) | 0.00 |

## How evidence was handled in this run

- Messages read: 198 (198 matched an intent pattern, 0 unmatched and therefore ignored).
- Images needed: 11 (cache hits: 11, OCR fallback: 0, unresolved: 0).
- `evidence_cache/image_amounts.json` holds the 16 amounts that were read from the images by Claude's vision during development (cross-checked with the *amount in words* line or OCR). That one-off, offline step is not part of the run above and is not counted in the token totals.
- Message and image content is treated as untrusted data: only dates, amounts and intents are extracted; no instruction inside a message can change the rules.
