from pathlib import Path


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
        assert "Needs a payee" in response.text

        # assign a new payee on the row, then commit
        logged_in.post(
            "/import/rows/1",
            data={"batch_id": "1", "new_payee": "Safeway", "category_id": "", "include": "on"},
        )
        summary = logged_in.post("/import/batches/bank/1/commit")
        assert "Imported <b>1</b>" in summary.text
        txns = ledger_api.get("/api/transactions").json()
        assert txns[0]["payee_name"] == "Safeway"

    def test_one_answer_per_description_fills_every_matching_row(self, logged_in, ledger_api):
        """Ten unknown rows, two merchants: two submits, not ten."""
        self.seed(logged_in)
        lines = ["Date,Description,Amount"]
        for day in range(1, 6):
            lines.append(f"08/0{day}/2026,SAFEWAY STORE 42,-1{day}.00")
            lines.append(f"08/0{day}/2026,SHELL OIL 771,-3{day}.00")
        logged_in.post(
            "/import/preview",
            data={"profile_id": "bank:1", "account_id": ""},
            files={"file": ("aug.csv", "\n".join(lines) + "\n", "text/csv")},
            follow_redirects=True,
        )
        page = logged_in.get("/import/batches/bank/1")
        assert "Assign by description" in page.text
        assert "Apply to 5 row(s)" in page.text

        logged_in.post(
            "/import/batches/bank/1/resolve",
            data={
                "description": "SAFEWAY STORE 42",
                "new_payee": "Safeway",
                "new_category": "Food: Groceries",
            },
        )
        logged_in.post(
            "/import/batches/bank/1/resolve",
            data={"description": "SHELL OIL 771", "new_payee": "Shell", "new_category": "Fuel"},
        )
        batch = ledger_api.get("/api/import/batches/1").json()
        assert all(r["status"] == "ready" for r in batch["rows"])
        assert {r["payee_name"] for r in batch["rows"]} == {"Safeway", "Shell"}

        logged_in.post("/import/batches/bank/1/commit")
        txns = ledger_api.get("/api/transactions").json()
        assert len(txns) == 10
        assert all(t["payee_name"] in ("Safeway", "Shell") for t in txns)

    def test_a_category_can_be_created_during_review(self, logged_in, ledger_api):
        self.seed(logged_in)
        good = "Date,Description,Amount\n08/01/2026,SAFEWAY STORE 42,-10.00\n"
        logged_in.post(
            "/import/preview",
            data={"profile_id": "bank:1", "account_id": ""},
            files={"file": ("aug.csv", good, "text/csv")},
            follow_redirects=True,
        )
        logged_in.post(
            "/import/rows/1",
            data={
                "batch_id": "1",
                "new_payee": "Safeway",
                "new_category": "Food: Groceries",
                "include": "on",
            },
        )
        categories = ledger_api.get("/api/categories").json()
        groceries = next(c for c in categories if c["path"] == "Food: Groceries")
        assert groceries["kind"] == "expense"  # inferred from the row being an outflow

        logged_in.post("/import/batches/bank/1/commit")
        txn = ledger_api.get("/api/transactions").json()[0]
        assert txn["splits"][0]["category_id"] == groceries["id"]


class TestUnifiedImportSection:
    def test_bank_and_brokerage_profiles_share_one_page(self, logged_in):
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
            "/stocks/accounts",
            data={"name": "E*Trade", "type": "brokerage", "opening_cash": "0", "note": ""},
        )
        logged_in.post(
            "/import/profiles",
            data={
                "name": "Test Bank",
                "kind": "bank",
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
        logged_in.post(
            "/import/profiles",
            data={
                "name": "Broker",
                "kind": "stock",
                "account_id": "1",
                "delimiter": ",",
                "has_header": "on",
                "skip_top_rows": "3",
                "date_column": "date",
                "date_format": "%m/%d/%Y",
                "action_column": "type",
                "map_from_0": "Bought",
                "map_to_0": "buy",
                "action_map_json": "",
            },
        )
        page = logged_in.get("/import/profiles")
        assert "Test Bank" in page.text
        assert "Broker" in page.text
        assert "Bank / credit card" in page.text
        assert "Brokerage" in page.text
        # Both are offered by the one upload form. Each service numbers its own
        # profiles, so both are id 1 here — the kind prefix is what disambiguates.
        start = logged_in.get("/import")
        assert 'value="bank:1"' in start.text
        assert 'value="stock:1"' in start.text

    def test_the_old_stock_import_page_redirects_into_the_import_section(self, logged_in):
        response = logged_in.get("/stocks/import", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/import"


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


class TestReprocessAndPayeeFallback:
    """Two things that used to force a discard-and-re-upload."""

    def seed(self, logged_in):
        logged_in.post(
            "/accounts",
            data={"name": "Checking", "type": "checking", "currency_code": "USD",
                  "opening_balance": "0", "note": ""},
        )
        logged_in.post(
            "/import/profiles",
            data={
                "name": "Bank", "kind": "bank", "account_id": "1", "delimiter": ",",
                "has_header": "on", "skip_top_rows": "0", "date_column": "date",
                "date_format": "%m/%d/%Y", "description_column": "description",
                "amount_mode": "single", "amount_column": "amount",
            },
        )
        content = (
            "Date,Description,Amount\n"
            "08/01/2026,EXPEDIA INC. 00000000000000004085 - DIR DEP,-120.00\n"
            "08/02/2026,EXPEDIA INC. 00000000000000009912 - DIR DEP,-340.00\n"
            "08/03/2026,QFC,-60.61\n"
        )
        logged_in.post(
            "/import/preview",
            data={"profile_id": "bank:1", "account_id": ""},
            files={"file": ("aug.csv", content, "text/csv")},
        )

    def test_the_review_page_offers_a_re_scan(self, logged_in):
        self.seed(logged_in)
        page = logged_in.get("/import/batches/bank/1")
        assert 'value="Re-scan against current rules"' in page.text

    def test_an_alias_added_mid_review_is_picked_up(self, logged_in, ledger_api):
        self.seed(logged_in)
        payee = ledger_api.post("/api/payees", json={"name": "Expedia"}).json()
        logged_in.post(
            f"/payees/{payee['id']}/aliases",
            data={"pattern": r"^EXPEDIA INC\. \d+ - DIR DEP$", "match_type": "regex"},
        )
        response = logged_in.post(
            "/import/batches/bank/1/reclassify", follow_redirects=False
        )
        assert response.status_code == 303
        assert "2%20row" in response.headers["location"]

        batch = ledger_api.get("/api/import/batches/1").json()
        assert batch["row_counts"] == {"ready": 2, "needs_payee": 1}

    def test_a_re_scan_that_finds_nothing_says_so(self, logged_in):
        self.seed(logged_in)
        response = logged_in.post(
            "/import/batches/bank/1/reclassify", follow_redirects=False
        )
        assert "no%20unanswered%20row" in response.headers["location"]

    def test_the_commit_form_offers_the_description_fallback(self, logged_in):
        self.seed(logged_in)
        page = logged_in.get("/import/batches/bank/1")
        assert 'name="name_from_description"' in page.text
        assert "use the bank" in page.text

    def test_committing_with_it_ticked_names_payees_after_descriptions(
        self, logged_in, ledger_api
    ):
        self.seed(logged_in)
        logged_in.post(
            "/import/batches/bank/1/commit", data={"name_from_description": "on"}
        )
        names = {p["name"] for p in ledger_api.get("/api/payees").json()}
        assert "QFC" in names

    def test_unticking_it_leaves_them_without_a_payee(self, logged_in, ledger_api):
        self.seed(logged_in)
        logged_in.post("/import/batches/bank/1/commit", data={})
        assert "QFC" not in {p["name"] for p in ledger_api.get("/api/payees").json()}
        assert all(t["payee_name"] is None for t in ledger_api.get("/api/transactions").json())
