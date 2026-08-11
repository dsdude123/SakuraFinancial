# receipts-service (:8004)

Receipt/invoice storage, parsing, and transaction linking. Live OpenAPI docs
at `/docs`.

## Pipeline

1. **Upload** (`POST /api/documents`): the original file is stored *forever*
   on the receipts volume; text is extracted (pdfplumber for PDFs,
   tesseract OCR for photos, tag-stripping for HTML invoices). Extraction
   never fails a request — worst case is a note like "PDF has no text layer".
2. **Parse** (automatic on upload; `POST /api/documents/{id}/parse` to
   re-run): if an LLM is configured in Settings, the text (or the image, for
   vision models) becomes vendor / date / total / currency plus line items
   with category guesses drawn from the ledger's real category list. No LLM →
   `needs_review` with the extracted text ready for manual entry.
3. **Match**: candidates are ledger transactions with the **exact amount
   within ±5 days**, ranked by vendor-name overlap. A scan auto-links
   documents with exactly one candidate; ambiguity waits for the user.
   The ledger pings `POST /api/match/scan` after every committed CSV import,
   so an Amazon charge imported today picks up the invoice uploaded last week.
4. **Apply split** (`POST /api/documents/{id}/apply-split`): the receipt's
   items become the category splits of the ONE linked transaction via the
   ledger API — never new transactions, total unchanged, and any difference
   shows up as a visible "(receipt rounding)" balancing line.

## Backup

`GET /api/export` (metadata + items as JSON) and `GET /api/export/files.zip`
(the original documents). Restore with `POST /api/import` and
`POST /api/import/files`. The web UI's Backup page bundles both.

## Future work

Pulling scans directly from a networked scanner (eSCL/AirScan) into the
upload pool — the `source` column already distinguishes `upload`/`scanner`.
