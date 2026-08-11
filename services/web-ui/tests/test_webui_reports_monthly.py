PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def seed_basic_data(ledger_api):
    ledger_api.post(
        "/api/accounts",
        json={"name": "Checking", "type": "checking", "opening_balance": "1000.00"},
    )
    salary = ledger_api.post("/api/categories", json={"name": "Salary", "kind": "income"}).json()
    rent = ledger_api.post("/api/categories", json={"name": "Rent", "kind": "expense"}).json()
    ledger_api.post(
        "/api/transactions",
        json={
            "account_id": 1,
            "date": "2026-08-01",
            "splits": [{"category_id": salary["id"], "amount": "5000.00"}],
        },
    )
    ledger_api.post(
        "/api/transactions",
        json={
            "account_id": 1,
            "date": "2026-08-02",
            "splits": [{"category_id": rent["id"], "amount": "-2000.00"}],
        },
    )


class TestCharts:
    def test_spending_chart_is_a_real_png(self, logged_in, ledger_api):
        seed_basic_data(ledger_api)
        response = logged_in.get("/charts/spending.png?month=2026-08")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(PNG_SIGNATURE)

    def test_cashflow_chart_renders(self, logged_in, ledger_api):
        seed_basic_data(ledger_api)
        response = logged_in.get("/charts/cashflow.png?months=3")
        assert response.content.startswith(PNG_SIGNATURE)

    def test_networth_chart_renders_without_stocks_service(self, logged_in, ledger_api):
        seed_basic_data(ledger_api)
        response = logged_in.get("/charts/networth.png?months=3")
        assert response.content.startswith(PNG_SIGNATURE)

    def test_empty_month_chart_still_renders(self, logged_in, ledger_api):
        response = logged_in.get("/charts/spending.png?month=1999-01")
        assert response.content.startswith(PNG_SIGNATURE)


class TestReportPages:
    def test_spending_report_nets_reimbursements(self, logged_in, ledger_api):
        seed_basic_data(ledger_api)
        # roommate pays back 800 against Rent
        ledger_api.post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": "2026-08-03",
                "splits": [{"category_id": 2, "amount": "800.00"}],
            },
        )
        page = logged_in.get("/reports/spending?month=2026-08")
        assert "Rent" in page.text
        assert "1,200.00" in page.text  # 2000 - 800 net

    def test_cashflow_report_page(self, logged_in, ledger_api):
        seed_basic_data(ledger_api)
        page = logged_in.get("/reports/cashflow?months=3")
        assert "5,000.00" in page.text
        assert "2,000.00" in page.text

    def test_networth_report_shows_missing_rate_warning(self, logged_in, ledger_api):
        ledger_api.post(
            "/api/accounts",
            json={
                "name": "TWD stash",
                "type": "cash",
                "currency_code": "TWD",
                "opening_balance": "10000.00",
            },
        )
        page = logged_in.get("/reports/networth?months=2")
        assert "No exchange rate" in page.text
        assert "TWD" in page.text

    def test_reports_hub(self, logged_in):
        page = logged_in.get("/reports")
        assert "Spending by category" in page.text
        assert "Net worth" in page.text


class TestMonthlyUpdates:
    def seed(self, logged_in, ledger_api):
        ledger_api.post("/api/accounts", json={"name": "Checking", "type": "checking"})
        ledger_api.post("/api/accounts", json={"name": "CAD wallet", "type": "cash", "currency_code": "CAD"})
        ledger_api.post("/api/accounts", json={"name": "Car", "type": "asset"})
        ledger_api.post(
            "/api/import/profiles",
            json={
                "name": "Bank",
                "account_id": 1,
                "config": {
                    "date_column": "date",
                    "description_column": "description",
                    "amount_column": "amount",
                },
            },
        )

    def test_cashflow_accounts_listed_assets_not(self, logged_in, ledger_api):
        self.seed(logged_in, ledger_api)
        page = logged_in.get("/monthly?month=2026-08")
        assert "Checking" in page.text
        assert "CAD wallet" in page.text
        assert "Car" not in page.text
        assert "0 of 2" in page.text

    def test_skip_and_unskip_persist_via_settings(self, logged_in, ledger_api):
        self.seed(logged_in, ledger_api)
        logged_in.post(
            "/monthly/2026-08/skip", data={"service": "ledger", "account_id": "2"}
        )
        page = logged_in.get("/monthly?month=2026-08")
        assert "skipped" in page.text
        assert "1 of 2" in page.text
        # a different month is unaffected
        other = logged_in.get("/monthly?month=2026-09")
        assert "0 of 2" in other.text

        logged_in.post(
            "/monthly/2026-08/unskip", data={"service": "ledger", "account_id": "2"}
        )
        assert "0 of 2" in logged_in.get("/monthly?month=2026-08").text

    def test_committed_import_marks_account_imported(self, logged_in, ledger_api):
        self.seed(logged_in, ledger_api)
        batch = ledger_api.post(
            "/api/import/preview",
            json={
                "profile_id": 1,
                "filename": "aug.csv",
                "content": "Date,Description,Amount\n08/01/2026,STORE,-1.00\n",
            },
        ).json()
        ledger_api.post(f"/api/import/batches/{batch['id']}/commit")
        page = logged_in.get("/monthly")
        assert "imported" in page.text
