RECEIPT_TEXT = b"""Amazon.com order
USB-C cable 12.99
Paper towels 24.99
Order total: $45.10
"""


def seed_transaction(ledger_api, amount="-45.10", date="2026-08-03", memo="AMAZON MKTP"):
    ledger_api.post("/api/accounts", json={"name": "Checking", "type": "checking"})
    return ledger_api.post(
        "/api/transactions",
        json={
            "account_id": 1,
            "date": date,
            "memo": memo,
            "splits": [{"category_id": None, "amount": amount}],
        },
    ).json()


class TestReceiptsPages:
    def test_upload_lands_in_inbox(self, logged_in):
        response = logged_in.post(
            "/receipts/upload",
            files={"file": ("amazon.txt", RECEIPT_TEXT, "text/plain")},
            follow_redirects=True,
        )
        assert "amazon.txt" in response.text
        inbox = logged_in.get("/receipts")
        assert "amazon.txt" in inbox.text

    def test_detail_edit_and_manual_link_flow(self, logged_in, ledger_api):
        txn = seed_transaction(ledger_api)
        logged_in.post(
            "/receipts/upload", files={"file": ("amazon.txt", RECEIPT_TEXT, "text/plain")}
        )
        # fill in details so candidates can be found
        logged_in.post(
            "/receipts/1/update",
            data={"vendor": "Amazon", "doc_date": "2026-08-02", "total": "45.10"},
        )
        page = logged_in.get("/receipts/1")
        assert "AMAZON MKTP" in page.text  # candidate row shown
        logged_in.post("/receipts/1/link", data={"transaction_id": str(txn["id"])})
        page = logged_in.get("/receipts/1")
        assert f"Linked to" in page.text

    def test_items_and_apply_split_single_transaction(self, logged_in, ledger_api):
        txn = seed_transaction(ledger_api)
        groceries = ledger_api.post(
            "/api/categories", json={"name": "Groceries", "kind": "expense"}
        ).json()
        household = ledger_api.post(
            "/api/categories", json={"name": "Household", "kind": "expense"}
        ).json()
        logged_in.post(
            "/receipts/upload", files={"file": ("amazon.txt", RECEIPT_TEXT, "text/plain")}
        )
        logged_in.post(
            "/receipts/1/update",
            data={"vendor": "Amazon", "doc_date": "2026-08-02", "total": "45.10"},
        )
        logged_in.post("/receipts/1/link", data={"transaction_id": str(txn["id"])})
        logged_in.post(
            "/receipts/1/items",
            data={
                "item_desc_0": "USB-C cable",
                "item_amount_0": "14.05",
                "item_category_0": str(household["id"]),
                "item_desc_1": "Paper towels",
                "item_amount_1": "31.05",
                "item_category_1": str(groceries["id"]),
            },
        )
        response = logged_in.post("/receipts/1/apply-split", follow_redirects=True)
        assert "2 category line(s)" in response.text

        updated = ledger_api.get(f"/api/transactions/{txn['id']}").json()
        assert updated["total"] == "-45.10"
        assert len(updated["splits"]) == 2
        assert len(ledger_api.get("/api/transactions").json()) == 1  # still ONE transaction

    def test_scan_button(self, logged_in, ledger_api):
        logged_in.post(
            "/receipts/upload", files={"file": ("amazon.txt", RECEIPT_TEXT, "text/plain")}
        )
        logged_in.post(
            "/receipts/1/update",
            data={"vendor": "Amazon", "doc_date": "2026-08-02", "total": "45.10"},
        )
        seed_transaction(ledger_api)
        response = logged_in.post("/receipts/scan", follow_redirects=True)
        assert "linked 1" in response.text

    def test_home_shows_receipt_todo(self, logged_in):
        logged_in.post(
            "/receipts/upload", files={"file": ("amazon.txt", RECEIPT_TEXT, "text/plain")}
        )
        page = logged_in.get("/")
        assert "receipt(s) waiting to be linked" in page.text
