import json

import sakura_common.llm

AMAZON_RECEIPT = b"""Amazon.com order #114-2233445
Order placed August 2, 2026
USB-C cable        12.99
Paper towels 12pk  24.99
Order total: $45.10
(includes tax 7.12)
"""


def configure_llm(settings, monkeypatch, reply: str):
    settings.values.update(
        {"llm.provider": "anthropic", "llm.api_key": "sk-test", "llm.model": "claude-test"}
    )
    monkeypatch.setattr(
        sakura_common.llm.LLMClient, "complete", lambda self, *a, **k: reply
    )


LLM_REPLY = json.dumps(
    {
        "vendor": "Amazon",
        "date": "2026-08-02",
        "total": "45.10",
        "currency": "USD",
        "items": [
            {"description": "USB-C cable", "amount": "14.05", "category": "Household"},
            {"description": "Paper towels 12pk", "amount": "31.05", "category": "Groceries"},
        ],
    }
)


class TestUploadAndExtraction:
    def test_plain_text_extracted(self, upload):
        document = upload(AMAZON_RECEIPT).json()
        assert "USB-C cable" in document["extracted_text"]

    def test_html_stripped(self, upload):
        html = b"<html><body><h1>Safeway</h1><p>Total: <b>$87.55</b></p><script>evil()</script></body></html>"
        document = upload(html, filename="invoice.html", content_type="text/html").json()
        assert "Safeway" in document["extracted_text"]
        assert "87.55" in document["extracted_text"]
        assert "evil" not in document["extracted_text"]

    def test_without_llm_needs_review_with_note(self, upload):
        document = upload(AMAZON_RECEIPT).json()
        assert document["status"] == "needs_review"
        assert "no AI provider configured" in document["parse_note"]

    def test_original_file_stored_and_served(self, client, upload):
        document = upload(AMAZON_RECEIPT).json()
        response = client.get(f"/api/documents/{document['id']}/file")
        assert response.content == AMAZON_RECEIPT

    def test_empty_file_rejected(self, upload):
        assert upload(b"").status_code == 422


class TestLLMParsing:
    def test_parse_fills_fields_and_split_items(
        self, upload, settings, monkeypatch, groceries, household
    ):
        configure_llm(settings, monkeypatch, LLM_REPLY)
        document = upload(AMAZON_RECEIPT).json()
        assert document["status"] == "parsed"
        assert document["vendor"] == "Amazon"
        assert document["doc_date"] == "2026-08-02"
        assert document["total"] == "45.10"
        assert len(document["items"]) == 2
        # category names resolved to real ledger category ids
        by_name = {item["category_name"]: item for item in document["items"]}
        assert by_name["Groceries"]["category_id"] == groceries["id"]
        assert by_name["Household"]["category_id"] == household["id"]

    def test_garbage_reply_needs_review(self, upload, settings, monkeypatch):
        configure_llm(settings, monkeypatch, "I cannot read this receipt, sorry!")
        document = upload(AMAZON_RECEIPT).json()
        assert document["status"] == "needs_review"
        assert "not JSON" in document["parse_note"]

    def test_manual_reparse_endpoint(self, client, upload, settings, monkeypatch):
        document = upload(AMAZON_RECEIPT).json()
        assert document["status"] == "needs_review"
        configure_llm(settings, monkeypatch, LLM_REPLY)
        reparsed = client.post(f"/api/documents/{document['id']}/parse").json()
        assert reparsed["status"] == "parsed"


class TestMatching:
    def seed_txn(self, ledger_api, checking, amount="-45.10", date="2026-08-03", memo="AMAZON MKTP"):
        return ledger_api.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": date,
                "memo": memo,
                "splits": [{"category_id": None, "amount": amount}],
            },
        ).json()

    def test_candidates_exact_amount_within_window(
        self, client, upload, settings, monkeypatch, ledger_api, checking
    ):
        configure_llm(settings, monkeypatch, LLM_REPLY)
        txn = self.seed_txn(ledger_api, checking)
        self.seed_txn(ledger_api, checking, amount="-99.99", memo="OTHER")
        document = upload(AMAZON_RECEIPT).json()
        candidates = client.get(f"/api/documents/{document['id']}/candidates").json()
        assert [c["id"] for c in candidates] == [txn["id"]]

    def test_upload_auto_links_single_candidate(
        self, client, upload, settings, monkeypatch, ledger_api, checking
    ):
        txn = self.seed_txn(ledger_api, checking)
        configure_llm(settings, monkeypatch, LLM_REPLY)
        document = upload(AMAZON_RECEIPT).json()
        # the upload flow runs a scan when parsing succeeds
        refreshed = client.get(f"/api/documents/{document['id']}").json()
        assert refreshed["status"] == "linked"
        assert refreshed["linked_transaction_id"] == txn["id"]

    def test_ambiguous_matches_stay_unlinked(
        self, client, upload, settings, monkeypatch, ledger_api, checking
    ):
        self.seed_txn(ledger_api, checking, date="2026-08-01")
        self.seed_txn(ledger_api, checking, date="2026-08-05")
        configure_llm(settings, monkeypatch, LLM_REPLY)
        document = upload(AMAZON_RECEIPT).json()
        refreshed = client.get(f"/api/documents/{document['id']}").json()
        assert refreshed["status"] == "parsed"
        assert refreshed["linked_transaction_id"] is None

    def test_scan_endpoint_links_after_import(
        self, client, upload, settings, monkeypatch, ledger_api, checking
    ):
        configure_llm(settings, monkeypatch, LLM_REPLY)
        document = upload(AMAZON_RECEIPT).json()
        assert client.get(f"/api/documents/{document['id']}").json()["status"] == "parsed"
        # the matching transaction arrives later (e.g. via CSV import)...
        self.seed_txn(ledger_api, checking)
        # ...and ledger pings the scan endpoint
        result = client.post("/api/match/scan").json()
        assert result["linked"] == 1
        assert client.get(f"/api/documents/{document['id']}").json()["status"] == "linked"

    def test_manual_link_and_unlink(
        self, client, upload, ledger_api, checking
    ):
        txn = self.seed_txn(ledger_api, checking)
        document = upload(AMAZON_RECEIPT).json()
        linked = client.post(
            f"/api/documents/{document['id']}/link", json={"transaction_id": txn["id"]}
        ).json()
        assert linked["status"] == "linked"
        unlinked = client.post(f"/api/documents/{document['id']}/unlink").json()
        assert unlinked["linked_transaction_id"] is None


class TestApplySplit:
    def test_split_divides_the_one_transaction(
        self, client, upload, settings, monkeypatch, ledger_api, checking, groceries, household
    ):
        """The user's requirement, verbatim: a receipt split must re-divide the
        single matched transaction across categories — never create new
        transactions or change the total."""
        txn = ledger_api.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-08-03",
                "memo": "AMAZON MKTP",
                "splits": [{"category_id": None, "amount": "-45.10"}],
            },
        ).json()
        configure_llm(settings, monkeypatch, LLM_REPLY)
        document = upload(AMAZON_RECEIPT).json()
        assert client.get(f"/api/documents/{document['id']}").json()["status"] == "linked"

        result = client.post(f"/api/documents/{document['id']}/apply-split").json()
        assert result["transaction_id"] == txn["id"]
        assert result["remainder"] == "0.00"

        updated = ledger_api.get(f"/api/transactions/{txn['id']}").json()
        assert updated["total"] == "-45.10"  # unchanged
        assert len(updated["splits"]) == 2  # one transaction, two categories
        amounts = {s["category_name"]: s["amount"] for s in updated["splits"]}
        assert amounts["Household"] == "-14.05"
        assert amounts["Groceries"] == "-31.05"
        # still exactly ONE transaction in the register
        assert len(ledger_api.get("/api/transactions").json()) == 1

    def test_rounding_remainder_becomes_balancing_line(
        self, client, upload, settings, monkeypatch, ledger_api, checking, groceries, household
    ):
        ledger_api.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-08-03",
                "splits": [{"category_id": None, "amount": "-50.00"}],
            },
        )
        reply = json.dumps(
            {
                "vendor": "Amazon",
                "date": "2026-08-02",
                "total": "50.00",
                "items": [{"description": "Cable", "amount": "45.00", "category": "Household"}],
            }
        )
        configure_llm(settings, monkeypatch, reply)
        document = upload(AMAZON_RECEIPT).json()
        result = client.post(f"/api/documents/{document['id']}/apply-split").json()
        assert result["remainder"] == "-5.00"
        updated = ledger_api.get(f"/api/transactions/{result['transaction_id']}").json()
        assert updated["total"] == "-50.00"
        memos = [s["memo"] for s in updated["splits"]]
        assert "(receipt rounding)" in memos

    def test_apply_without_link_rejected(self, client, upload):
        document = upload(AMAZON_RECEIPT).json()
        response = client.post(f"/api/documents/{document['id']}/apply-split")
        assert response.status_code == 422


class TestExportImport:
    def test_metadata_round_trip_and_files_zip(self, client, upload):
        upload(AMAZON_RECEIPT).json()
        exported = client.get("/api/export").json()
        assert exported["service"] == "receipts"
        assert len(exported["documents"]) == 1

        files = client.get("/api/export/files.zip")
        assert files.headers["content-type"] == "application/zip"
        assert files.content[:2] == b"PK"

        response = client.post("/api/import", json=exported)
        assert response.json() == {"imported": 1}
        assert len(client.get("/api/documents").json()) == 1


class TestReset:
    def test_reset_clears_documents_and_their_files(self, client, upload, tmp_path):
        """Receipts owns files on disk as well as rows, so a reset that only
        emptied the tables would leave the volume full of orphaned scans."""
        upload(b"CORNER STORE\nTOTAL 12.34\n")
        upload(b"HARDWARE SHOP\nTOTAL 56.78\n")
        data_dir = tmp_path / "receipts-data"
        assert len(list(data_dir.iterdir())) == 2

        result = client.post("/api/reset").json()
        assert result["deleted"]["documents"] == 2
        assert result["files_removed"] == 2
        assert client.get("/api/documents").json() == []
        assert list(data_dir.iterdir()) == []

    def test_reset_on_an_empty_service_is_harmless(self, client):
        result = client.post("/api/reset").json()
        assert result["deleted"]["documents"] == 0
        assert client.get("/api/documents").json() == []
