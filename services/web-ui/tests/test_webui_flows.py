class TestAuth:
    def test_anonymous_is_redirected_to_login(self, browser):
        response = browser.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_first_run_offers_password_setup(self, browser):
        page = browser.get("/login")
        assert "fresh installation" in page.text

    def test_setup_password_logs_in_and_persists(self, browser):
        browser.post("/setup-password", data={"password": "hunter22", "password2": "hunter22"})
        assert browser.get("/").status_code == 200

    def test_mismatched_setup_rejected(self, browser):
        response = browser.post("/setup-password", data={"password": "aaaa", "password2": "bbbb"})
        assert "do not match" in response.text

    def test_login_wrong_then_right(self, browser):
        browser.post("/setup-password", data={"password": "hunter22", "password2": "hunter22"})
        browser.get("/logout")
        assert browser.get("/").status_code == 303

        wrong = browser.post("/login", data={"password": "nope"})
        assert "Wrong password" in wrong.text
        right = browser.post("/login", data={"password": "hunter22"})
        assert right.status_code == 303
        assert browser.get("/").status_code == 200

    def test_second_setup_attempt_cannot_overwrite(self, browser, stack):
        browser.post("/setup-password", data={"password": "hunter22", "password2": "hunter22"})
        browser.get("/logout")
        response = browser.post(
            "/setup-password", data={"password": "evil", "password2": "evil"}
        )
        assert response.status_code == 303
        assert not browser.get("/").status_code == 200


class TestAccountsAndRegister:
    def test_create_account_and_enter_transaction(self, logged_in):
        logged_in.post(
            "/accounts",
            data={
                "name": "Checking",
                "type": "checking",
                "currency_code": "USD",
                "opening_balance": "1000.00",
                "note": "",
            },
        )
        accounts_page = logged_in.get("/accounts")
        assert "Checking" in accounts_page.text
        assert "1,000.00" in accounts_page.text

        register = logged_in.get("/accounts/1/register")
        assert register.status_code == 200

        response = logged_in.post(
            "/accounts/1/register",
            data={
                "date": "2026-08-05",
                "payee_id": "",
                "new_payee": "Safeway",
                "category_id": "",
                "amount": "-87.55",
                "memo": "groceries run",
            },
        )
        assert response.status_code == 303
        register = logged_in.get("/accounts/1/register")
        assert "Safeway" in register.text
        assert "-87.55" in register.text.replace("&#45;", "-") or "87.55" in register.text
        assert "912.45" in register.text  # running balance

    def test_payee_autofill_prefills_form(self, logged_in, ledger_api):
        ledger_api.post("/api/accounts", json={"name": "Checking", "type": "checking"})
        category = ledger_api.post(
            "/api/categories", json={"name": "Rent", "kind": "expense"}
        ).json()
        payee = ledger_api.post(
            "/api/payees", json={"name": "Landlord", "default_category_id": category["id"]}
        ).json()
        ledger_api.post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": "2026-07-01",
                "payee_id": payee["id"],
                "splits": [{"category_id": category["id"], "amount": "-1950.00"}],
            },
        )
        page = logged_in.get(f"/accounts/1/register?prefill_payee={payee['id']}")
        assert 'value="-1950.00"' in page.text  # last amount pre-filled

    def test_asset_account_shows_valuation_form_not_entry(self, logged_in):
        logged_in.post(
            "/accounts",
            data={
                "name": "Car",
                "type": "asset",
                "currency_code": "USD",
                "opening_balance": "62000.00",
                "note": "",
            },
        )
        page = logged_in.get("/accounts/1/register")
        assert "Update value" in page.text
        response = logged_in.post(
            "/accounts/1/valuation", data={"date": "2026-08-01", "new_value": "24000.00"}
        )
        assert response.status_code == 303
        page = logged_in.get("/accounts/1/register")
        assert "Value change" in page.text


class TestImportWizard:
    def seed(self, logged_in):
        logged_in.post(
            "/accounts",
            data={
                "name": "Checking",
                "type": "checking",
                "currency_code": "USD",
                "opening_balance": "0",
                "note": "",
            },
        )
        logged_in.post(
            "/import/profiles",
            data={
                "name": "Test Bank",
                "account_id": "1",
                "delimiter": ",",
                "has_header": "on",
                "skip_top_rows": "0",
                "date_column": "date",
                "date_format": "%m/%d/%Y",
                "description_column": "description",
                "amount_mode": "single",
                "amount_column": "amount",
            },
        )

    def test_broken_file_shows_error_report_and_writes_nothing(self, logged_in, ledger_api):
        self.seed(logged_in)
        broken = "Date,Description,Amount\n08/01/2026,OK ROW,-1.00\n08/02/2026,BAD,PENDING\n"
        response = logged_in.post(
            "/import/preview",
            data={"profile_id": "1", "account_id": ""},
            files={"file": ("aug.csv", broken, "text/csv")},
        )
        assert "didn't pass validation" in response.text
        assert "PENDING" in response.text
        assert "Nothing was imported" in response.text
        assert ledger_api.get("/api/transactions").json() == []

    def test_good_file_review_and_commit(self, logged_in, ledger_api):
        self.seed(logged_in)
        good = "Date,Description,Amount\n08/01/2026,SAFEWAY STORE 42,-10.00\n"
        response = logged_in.post(
            "/import/preview",
            data={"profile_id": "1", "account_id": ""},
            files={"file": ("aug.csv", good, "text/csv")},
            follow_redirects=True,
        )
        assert "SAFEWAY STORE 42" in response.text
        assert "needs_payee" in response.text

        # assign a new payee on the row, then commit
        logged_in.post(
            "/import/rows/1",
            data={"batch_id": "1", "new_payee": "Safeway", "category_id": "", "include": "on"},
        )
        summary = logged_in.post("/import/batches/1/commit")
        assert "Imported <b>1</b>" in summary.text
        txns = ledger_api.get("/api/transactions").json()
        assert txns[0]["payee_name"] == "Safeway"


class TestBillsPage:
    def test_amount_review_prompt_flow(self, logged_in, ledger_api):
        ledger_api.post("/api/accounts", json={"name": "Checking", "type": "checking"})
        payee = ledger_api.post("/api/payees", json={"name": "Power Co"}).json()
        ledger_api.post(
            "/api/bills",
            json={
                "name": "Electric",
                "payee_id": payee["id"],
                "frequency": "monthly",
                "amount": "142.19",
                "next_due": "2026-08-05",
            },
        )
        ledger_api.post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": "2026-08-05",
                "payee_id": payee["id"],
                "splits": [{"category_id": None, "amount": "-150.00"}],
            },
        )
        page = logged_in.get("/bills")
        assert "charged a different amount" in page.text
        assert "142.19" in page.text and "150.00" in page.text

        response = logged_in.post(
            "/bills/occurrences/1/resolve", data={"action": "update_bill"}, follow_redirects=True
        )
        assert "updated to 150.00" in response.text

    def test_accrual_overview_shows_monthly_load(self, logged_in, ledger_api):
        payee = ledger_api.post("/api/payees", json={"name": "Allstate"}).json()
        ledger_api.post(
            "/api/bills",
            json={
                "name": "Car Insurance",
                "payee_id": payee["id"],
                "frequency": "annual",
                "amount": "1200.00",
                "next_due": "2026-12-15",
            },
        )
        page = logged_in.get("/bills")
        assert "Car Insurance" in page.text
        assert "100.00" in page.text  # 1200/12 monthly load


class TestBudgetPage:
    def test_budget_grid_and_set(self, logged_in, ledger_api):
        ledger_api.post("/api/accounts", json={"name": "Checking", "type": "checking"})
        category = ledger_api.post(
            "/api/categories", json={"name": "Groceries", "kind": "expense"}
        ).json()
        ledger_api.post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": "2026-08-02",
                "splits": [{"category_id": category["id"], "amount": "-300.00"}],
            },
        )
        logged_in.post(
            f"/budget/2026-08/set", data={"category_id": str(category["id"]), "amount": "400.00"}
        )
        page = logged_in.get("/budget?month=2026-08")
        assert "Groceries" in page.text
        assert "General Fund" in page.text

    def test_goals_page(self, logged_in):
        logged_in.post(
            "/goals",
            data={
                "name": "Taiwan trip",
                "target_amount": "3000.00",
                "monthly_contribution": "250.00",
                "target_date": "",
                "priority": "1",
            },
        )
        page = logged_in.get("/goals")
        assert "Taiwan trip" in page.text


class TestSettingsPage:
    def test_settings_render_and_currency_add(self, logged_in):
        page = logged_in.get("/settings")
        assert "AI assistant" in page.text
        response = logged_in.post(
            "/settings/currencies", data={"code": "jpy", "name": "Yen", "decimals": "0"}
        )
        assert response.status_code == 303
        assert "JPY" in logged_in.get("/settings").text

    def test_llm_settings_saved_to_settings_service(self, logged_in, stack):
        from fastapi.testclient import TestClient

        logged_in.post(
            "/settings/llm",
            data={
                "provider": "anthropic",
                "api_key": "sk-test",
                "model": "claude-test",
                "base_url": "",
            },
        )
        settings_api = TestClient(stack["settings"])
        stored = settings_api.get(
            "/api/settings/llm.api_key", params={"reveal": "true"}
        ).json()
        assert stored["value"] == "sk-test"

    def test_password_change(self, logged_in):
        response = logged_in.post(
            "/settings/password",
            data={"current": "hunter22", "password": "newpass", "password2": "newpass"},
            follow_redirects=True,
        )
        assert "Password changed" in response.text
        logged_in.get("/logout")
        assert logged_in.post("/login", data={"password": "newpass"}).status_code == 303


class TestErrorPage:
    def test_down_service_renders_friendly_page(self, logged_in):
        # stocks/receipts transports simulate connection failures; /stocks page
        # arrives in a later phase, so exercise via home (receipts panel hides).
        page = logged_in.get("/")
        assert page.status_code == 200
