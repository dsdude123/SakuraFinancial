"""Moving cash between a bank account and a brokerage, through the real pages.

This is the movement the app could not express at all before: checking funds a
brokerage, and the two accounts are owned by different services. The web UI
makes one transfer out of two calls, so these tests run the whole stack and
check both books afterwards.
"""

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def ledger_api(stack):
    return TestClient(stack["ledger"])


@pytest.fixture()
def stocks_api(stack):
    return TestClient(stack["stocks"])


@pytest.fixture()
def checking(ledger_api):
    return ledger_api.post(
        "/api/accounts",
        json={"name": "Checking", "type": "checking", "opening_balance": "5000.00"},
    ).json()


@pytest.fixture()
def brokerage(stocks_api):
    return stocks_api.post(
        "/api/accounts",
        json={"name": "Fidelity", "type": "brokerage", "opening_cash": "1000.00"},
    ).json()


def bank_balance(ledger_api, account):
    return ledger_api.get(f"/api/accounts/{account['id']}").json()["balance"]


def brokerage_cash(stocks_api, account):
    return stocks_api.get(f"/api/accounts/{account['id']}").json()["cash"]


class TestFromTheRegister:
    def test_the_picker_offers_investment_accounts(
        self, logged_in, ledger_api, checking, brokerage
    ):
        savings = ledger_api.post(
            "/api/accounts", json={"name": "Savings", "type": "savings"}
        ).json()
        page = logged_in.get(f"/accounts/{checking['id']}/register")
        assert f'value="stock:{brokerage["id"]}"' in page.text
        assert "Fidelity" in page.text
        # the other bank accounts are still there, now namespaced too
        assert f'value="bank:{savings["id"]}"' in page.text

    def test_funding_a_brokerage_moves_both_sides(
        self, logged_in, ledger_api, stocks_api, checking, brokerage
    ):
        response = logged_in.post(
            f"/accounts/{checking['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"stock:{brokerage['id']}",
                "amount": "2500.00",
                "to_amount": "",
                "memo": "",
            },
            follow_redirects=True,
        )
        assert "Transfer to Fidelity saved" in response.text
        assert bank_balance(ledger_api, checking) == "2500.00"
        assert brokerage_cash(stocks_api, brokerage) == "3500.00"

        leg = ledger_api.get("/api/transactions").json()[0]
        far = stocks_api.get("/api/transactions").json()[0]
        assert leg["kind"] == "transfer"
        assert leg["external_account"] == f"stock:{brokerage['id']}"
        assert far["type"] == "deposit"
        assert far["external_account"] == f"bank:{checking['id']}"
        assert far["transfer_group_id"] == leg["transfer_group_id"]

    def test_taking_money_back_out(
        self, logged_in, ledger_api, stocks_api, checking, brokerage
    ):
        logged_in.post(
            f"/accounts/{checking['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "in",
                "other_account_id": f"stock:{brokerage['id']}",
                "amount": "400.00",
                "to_amount": "",
                "memo": "",
            },
        )
        assert bank_balance(ledger_api, checking) == "5400.00"
        assert brokerage_cash(stocks_api, brokerage) == "600.00"
        assert stocks_api.get("/api/transactions").json()[0]["type"] == "withdraw"

    def test_it_never_looks_like_spending(
        self, logged_in, ledger_api, checking, brokerage
    ):
        logged_in.post(
            f"/accounts/{checking['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"stock:{brokerage['id']}",
                "amount": "2500.00",
                "to_amount": "",
                "memo": "",
            },
        )
        page = logged_in.get("/reports/spending?month=2026-09")
        assert "2,500.00" not in page.text
        tree = ledger_api.get(
            "/api/reports/category-tree",
            params={"start": "2026-09-01", "end": "2026-09-30"},
        ).json()
        assert tree == []

    def test_the_register_marks_it_as_an_investment_transfer(
        self, logged_in, checking, brokerage
    ):
        logged_in.post(
            f"/accounts/{checking['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"stock:{brokerage['id']}",
                "amount": "2500.00",
                "to_amount": "",
                "memo": "",
            },
        )
        page = logged_in.get(f"/accounts/{checking['id']}/register")
        assert "investment" in page.text
        assert "Transfer to Fidelity" in page.text

    def test_bank_to_bank_transfers_still_work(self, logged_in, ledger_api, checking):
        savings = ledger_api.post(
            "/api/accounts", json={"name": "Savings", "type": "savings"}
        ).json()
        response = logged_in.post(
            f"/accounts/{checking['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"bank:{savings['id']}",
                "amount": "100.00",
                "to_amount": "",
                "memo": "",
            },
            follow_redirects=True,
        )
        assert "Transfer saved" in response.text
        assert bank_balance(ledger_api, checking) == "4900.00"
        assert bank_balance(ledger_api, savings) == "100.00"

    def test_net_worth_counts_the_money_once(
        self, logged_in, checking, brokerage
    ):
        before = logged_in.get("/reports/networth").text
        logged_in.post(
            f"/accounts/{checking['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"stock:{brokerage['id']}",
                "amount": "2500.00",
                "to_amount": "",
                "memo": "",
            },
        )
        after = logged_in.get("/reports/networth").text
        # 5000 cash + 1000 brokerage either way: the money changed hands, the
        # total did not.
        assert "6,000.00" in before
        assert "6,000.00" in after


class TestFromTheStockAccountPage:
    def test_the_page_offers_the_transfer(self, logged_in, checking, brokerage):
        page = logged_in.get(f"/stocks/accounts/{brokerage['id']}")
        assert "Transfer cash with a bank account" in page.text
        assert "Checking" in page.text

    def test_cash_in_from_checking(
        self, logged_in, ledger_api, stocks_api, checking, brokerage
    ):
        response = logged_in.post(
            f"/stocks/accounts/{brokerage['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "in",
                "bank_account_id": str(checking["id"]),
                "amount": "2500.00",
                "bank_amount": "",
                "memo": "",
            },
            follow_redirects=True,
        )
        assert "Transfer from Checking saved" in response.text
        assert bank_balance(ledger_api, checking) == "2500.00"
        assert brokerage_cash(stocks_api, brokerage) == "3500.00"

    def test_cash_out_to_checking(
        self, logged_in, ledger_api, stocks_api, checking, brokerage
    ):
        response = logged_in.post(
            f"/stocks/accounts/{brokerage['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "bank_account_id": str(checking["id"]),
                "amount": "250.00",
                "bank_amount": "",
                "memo": "",
            },
            follow_redirects=True,
        )
        assert "Transfer to Checking saved" in response.text
        assert bank_balance(ledger_api, checking) == "5250.00"
        assert brokerage_cash(stocks_api, brokerage) == "750.00"

    def test_without_an_account_it_says_so(self, logged_in, brokerage):
        response = logged_in.post(
            f"/stocks/accounts/{brokerage['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "in",
                "bank_account_id": "",
                "amount": "100.00",
                "bank_amount": "",
                "memo": "",
            },
            follow_redirects=True,
        )
        assert "Pick the bank account" in response.text


class TestCrossCurrency:
    def test_it_asks_for_the_other_side(self, logged_in, ledger_api, stocks_api):
        twd = ledger_api.post(
            "/api/accounts",
            json={
                "name": "Taipei Checking",
                "type": "checking",
                "currency_code": "TWD",
                "opening_balance": "100000.00",
            },
        ).json()
        usd_brokerage = stocks_api.post(
            "/api/accounts", json={"name": "Schwab", "type": "brokerage"}
        ).json()
        response = logged_in.post(
            f"/accounts/{twd['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"stock:{usd_brokerage['id']}",
                "amount": "31000.00",
                "to_amount": "",
                "memo": "",
            },
            follow_redirects=True,
        )
        assert "different currencies" in response.text
        assert ledger_api.get("/api/transactions").json() == []
        assert stocks_api.get("/api/transactions").json() == []

    def test_each_side_keeps_its_own_amount(self, logged_in, ledger_api, stocks_api):
        twd = ledger_api.post(
            "/api/accounts",
            json={
                "name": "Taipei Checking",
                "type": "checking",
                "currency_code": "TWD",
                "opening_balance": "100000.00",
            },
        ).json()
        usd_brokerage = stocks_api.post(
            "/api/accounts", json={"name": "Schwab", "type": "brokerage"}
        ).json()
        logged_in.post(
            f"/accounts/{twd['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"stock:{usd_brokerage['id']}",
                "amount": "31000.00",
                "to_amount": "1000.00",
                "memo": "",
            },
        )
        assert bank_balance(ledger_api, twd) == "69000.00"
        assert brokerage_cash(stocks_api, usd_brokerage) == "1000.00"


class TestWhenHalfOfItFails:
    """Two services, no shared transaction. The ledger leg is written first
    because it is the one we can take back."""

    def test_the_bank_leg_is_undone(
        self, logged_in, stack, ledger_api, stocks_api, checking, brokerage
    ):
        from webui_service.clients import ServiceError

        async def failing_post(path, json=None):
            raise ServiceError("stocks", 500, "stocks database is on fire")

        stack["webui"].state.clients.stocks.post = failing_post
        response = logged_in.post(
            f"/accounts/{checking['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"stock:{brokerage['id']}",
                "amount": "2500.00",
                "to_amount": "",
                "memo": "",
            },
            follow_redirects=True,
        )
        assert "nothing was saved" in response.text
        assert "on fire" in response.text
        # The compensating delete ran: no orphaned leg, balance untouched.
        assert ledger_api.get("/api/transactions").json() == []
        assert bank_balance(ledger_api, checking) == "5000.00"

    def test_a_dead_stocks_service_leaves_bank_transfers_alone(
        self, logged_in, stack, ledger_api, checking
    ):
        """The register page fetches investment accounts to fill its picker;
        losing the stocks service must not take the whole page down."""
        savings = ledger_api.post(
            "/api/accounts", json={"name": "Savings", "type": "savings"}
        ).json()

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("stocks is down")

        stack["webui"].state.clients.stocks._http = httpx.AsyncClient(
            transport=httpx.MockTransport(down)
        )
        page = logged_in.get(f"/accounts/{checking['id']}/register")
        assert page.status_code == 200
        assert 'value="stock:' not in page.text
        response = logged_in.post(
            f"/accounts/{checking['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"bank:{savings['id']}",
                "amount": "100.00",
                "to_amount": "",
                "memo": "",
            },
            follow_redirects=True,
        )
        assert "Transfer saved" in response.text


class TestDeleting:
    def test_deleting_the_bank_leg_removes_the_brokerage_side(
        self, logged_in, ledger_api, stocks_api, checking, brokerage
    ):
        logged_in.post(
            f"/accounts/{checking['id']}/transfer",
            data={
                "date": "2026-09-10",
                "direction": "out",
                "other_account_id": f"stock:{brokerage['id']}",
                "amount": "2500.00",
                "to_amount": "",
                "memo": "",
            },
        )
        leg = ledger_api.get("/api/transactions").json()[0]
        response = logged_in.post(
            f"/transactions/{leg['id']}/edit",
            data={"action_delete": "1"},
            follow_redirects=True,
        )
        assert "Transaction deleted" in response.text
        assert ledger_api.get("/api/transactions").json() == []
        assert stocks_api.get("/api/transactions").json() == []
        assert bank_balance(ledger_api, checking) == "5000.00"
        assert brokerage_cash(stocks_api, brokerage) == "1000.00"

    def test_deleting_an_ordinary_transaction_is_unaffected(
        self, logged_in, ledger_api, checking
    ):
        txn = ledger_api.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-09-10",
                "splits": [{"category_id": None, "amount": "-20.00"}],
            },
        ).json()
        response = logged_in.post(
            f"/transactions/{txn['id']}/edit",
            data={"action_delete": "1"},
            follow_redirects=True,
        )
        assert "Transaction deleted" in response.text
