# Real Estate Project Finance POC

A single-company, open-source POC that keeps project accounts in one place. Bills, invoices, bank statements, CSV/Excel ledgers and manual entries are turned into structured ledger transactions in PostgreSQL, and reports can be pulled at any time. The Django application provides the UI, backend, and admin.

## How data gets in

| Source | Formats | How it is read |
| --- | --- | --- |
| Bills / receipts, invoices | PDF, PNG, JPG, TIFF, WEBP | PDF text extraction (pdfplumber); scanned PDFs and photos use Tesseract OCR. Total, date, invoice number, vendor and GSTIN are detected heuristically. |
| Bank statements | CSV, XLSX, text-based PDF | Header row is auto-detected (e.g. *Txn Date, Narration, Withdrawal Amt., Deposit Amt.*). PDFs use table extraction, falling back to running-balance line parsing. |
| CSV / spreadsheet ledgers | CSV, XLSX | Needs a header with a date column and *Amount* (signed) or *Debit/Credit*. Optional: Vendor, Reference, Category, Project (matched to existing names/codes). |
| Manual entry | Web form | Posted directly to the ledger. |

Uploaded files are never posted automatically. Each upload creates *extracted entries* that you review, correct and approve on the document page; only approved entries become transactions. Re-uploading the same file is blocked, and rows matching an existing transaction (same account, date, amount, direction, description, reference) are flagged as possible duplicates.

Reports (filter by date range, project, account, category, direction, source or text) show totals by month, project (with budget remaining), category, account and top vendors, and can be exported to CSV.

Set up accounts (bank/cash/card with opening balance), categories and projects in the Django admin first.

## Requirements

- Docker Engine with Docker Compose v2
- PowerShell (commands below use Windows syntax)

## Start locally

1. Copy the example environment file and replace both placeholder secrets:

   ```powershell
   Copy-Item .env.example .env
   ```

   Generate a Django secret, for example with `python -c "import secrets; print(secrets.token_urlsafe(64))"`, then set `DJANGO_SECRET_KEY` in `.env`. Set a strong `POSTGRES_PASSWORD` too. Keep `.env` private; it is ignored by Git.

2. Build and start the application and database:

   ```powershell
   docker compose up --build -d
   ```

3. Create the first administrator:

   ```powershell
   docker compose exec web python manage.py createsuperuser
   ```

4. Open [http://localhost:8000](http://localhost:8000) and sign in. The dashboard links to the Django admin for creating projects and recording expenses.

## Network and data

- The web app is published only on `127.0.0.1:8000`, so it is not exposed on other network interfaces by default.
- PostgreSQL has no published host port. It is attached only to an internal Docker network shared with the web app.
- PostgreSQL data and uploaded media use named Docker volumes and survive container recreation.
- This POC does not yet include automated backup/restore, live bank integrations, or statutory tax calculations. Extraction is heuristic and always requires human review. Do not treat it as a production accounting system until those controls are implemented and tested.
- Uploads are limited to 10 MB by default (`DOCUMENT_MAX_UPLOAD_MB` in `.env`). Originals are stored in the private uploads volume and are only downloadable by signed-in users.

Useful commands:

```powershell
docker compose logs -f web
docker compose ps
docker compose down
```

`docker compose down -v` deletes the database and uploaded files; do not use it unless you intend to erase POC data.

## Tests

```powershell
docker compose exec web python manage.py test
```

## Cloud deployment

GitHub Actions ([.github/workflows/ci-cd.yml](.github/workflows/ci-cd.yml)) tests every change, builds an image to GHCR, deploys to a staging VM, runs the test suite and smoke tests there, and deploys to production after manual approval. Servers run `compose.yaml` + `compose.prod.yaml` with Caddy for automatic HTTPS. Step-by-step commands are in [docs/application-documentation.html](docs/application-documentation.html) section 17.
