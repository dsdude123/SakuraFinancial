import pytest
from fastapi.testclient import TestClient

from ledger_service.main import create_app


@pytest.fixture()
def client():
    app = create_app(database_url="sqlite://")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def checking(client):
    return client.post(
        "/api/accounts",
        json={"name": "Checking", "type": "checking", "opening_balance": "1000.00"},
    ).json()


@pytest.fixture()
def savings(client):
    return client.post("/api/accounts", json={"name": "Savings", "type": "savings"}).json()


@pytest.fixture()
def ledger_third_account(client):
    return client.post("/api/accounts", json={"name": "Brokerage Cash", "type": "cash"}).json()


@pytest.fixture()
def rent_category(client):
    return client.post("/api/categories", json={"name": "Rent", "kind": "expense"}).json()


@pytest.fixture()
def salary_category(client):
    return client.post("/api/categories", json={"name": "Salary", "kind": "income"}).json()


@pytest.fixture()
def groceries_category(client):
    return client.post("/api/categories", json={"name": "Groceries", "kind": "expense"}).json()


@pytest.fixture()
def landlord(client, rent_category):
    return client.post(
        "/api/payees",
        json={"name": "Landlord LLC", "default_category_id": rent_category["id"]},
    ).json()


@pytest.fixture()
def make_txn(client):
    def _make(account_id, date, amount=None, category_id=None, payee_id=None, memo="", splits=None):
        body = {
            "account_id": account_id,
            "date": date,
            "payee_id": payee_id,
            "memo": memo,
            "splits": splits
            if splits is not None
            else [{"category_id": category_id, "amount": amount}],
        }
        response = client.post("/api/transactions", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    return _make
