"""Subcategories: "Food" broken into Groceries vs Dining Out, the way MS Money
did it — including the rollup that makes them worth having."""

import pytest


@pytest.fixture()
def food(client):
    return client.post("/api/categories", json={"name": "Food", "kind": "expense"}).json()


@pytest.fixture()
def groceries_sub(client, food):
    return client.post(
        "/api/categories",
        json={"name": "Groceries", "kind": "expense", "parent_id": food["id"]},
    ).json()


@pytest.fixture()
def dining(client, food):
    return client.post(
        "/api/categories",
        json={"name": "Dining Out", "kind": "expense", "parent_id": food["id"]},
    ).json()


class TestStructure:
    def test_child_reports_its_full_path(self, client, groceries_sub):
        listed = {c["id"]: c for c in client.get("/api/categories").json()}
        assert listed[groceries_sub["id"]]["path"] == "Food: Groceries"
        assert listed[groceries_sub["id"]]["parent_name"] == "Food"

    def test_parent_path_is_just_its_name(self, client, food):
        listed = {c["id"]: c for c in client.get("/api/categories").json()}
        assert listed[food["id"]]["path"] == "Food"

    def test_children_are_listed_directly_after_their_parent(
        self, client, food, groceries_sub, dining
    ):
        client.post("/api/categories", json={"name": "Auto", "kind": "expense"})
        order = [c["path"] for c in client.get("/api/categories").json() if c["kind"] == "expense"]
        assert order == ["Auto", "Food", "Food: Dining Out", "Food: Groceries"]

    def test_only_one_level_deep(self, client, groceries_sub):
        response = client.post(
            "/api/categories",
            json={"name": "Organic", "kind": "expense", "parent_id": groceries_sub["id"]},
        )
        assert response.status_code == 422
        assert "one level deep" in response.json()["detail"]

    def test_same_child_name_allowed_under_different_parents(self, client, food, groceries_sub):
        pets = client.post("/api/categories", json={"name": "Pets", "kind": "expense"}).json()
        response = client.post(
            "/api/categories",
            json={"name": "Groceries", "kind": "expense", "parent_id": pets["id"]},
        )
        assert response.status_code == 200
        paths = [c["path"] for c in client.get("/api/categories").json()]
        assert "Food: Groceries" in paths
        assert "Pets: Groceries" in paths

    def test_seed_defaults_ships_a_nested_food_category(self, client):
        client.post("/api/seed-defaults")
        paths = [c["path"] for c in client.get("/api/categories").json()]
        assert "Food" in paths
        assert "Food: Groceries" in paths
        assert "Food: Dining Out" in paths


class TestRollup:
    def test_parent_totals_its_children(
        self, client, checking, food, groceries_sub, dining, make_txn
    ):
        make_txn(checking["id"], "2026-08-02", "-600.00", groceries_sub["id"])
        make_txn(checking["id"], "2026-08-09", "-250.00", dining["id"])

        tree = client.get(
            "/api/reports/category-tree", params={"start": "2026-08-01", "end": "2026-08-31"}
        ).json()
        food_node = next(node for node in tree if node["category_id"] == food["id"])
        assert food_node["net"] == "-850.00"
        assert food_node["own_net"] == "0.00"
        assert [(c["name"], c["net"]) for c in food_node["children"]] == [
            ("Dining Out", "-250.00"),
            ("Groceries", "-600.00"),
        ]

    def test_parent_direct_spending_included_in_its_total(
        self, client, checking, food, groceries_sub, make_txn
    ):
        make_txn(checking["id"], "2026-08-02", "-600.00", groceries_sub["id"])
        make_txn(checking["id"], "2026-08-03", "-40.00", food["id"])  # uncategorized food run

        tree = client.get(
            "/api/reports/category-tree", params={"start": "2026-08-01", "end": "2026-08-31"}
        ).json()
        food_node = next(node for node in tree if node["category_id"] == food["id"])
        assert food_node["own_net"] == "-40.00"
        assert food_node["net"] == "-640.00"

    def test_parent_appears_even_with_no_direct_spending_of_its_own(
        self, client, checking, food, groceries_sub, make_txn
    ):
        make_txn(checking["id"], "2026-08-02", "-600.00", groceries_sub["id"])
        tree = client.get(
            "/api/reports/category-tree", params={"start": "2026-08-01", "end": "2026-08-31"}
        ).json()
        assert any(node["category_id"] == food["id"] for node in tree)

    def test_reimbursement_nets_within_the_subcategory(
        self, client, checking, food, groceries_sub, dining, make_txn
    ):
        make_txn(checking["id"], "2026-08-02", "-600.00", groceries_sub["id"])
        make_txn(checking["id"], "2026-08-04", "100.00", groceries_sub["id"])  # split with a friend
        make_txn(checking["id"], "2026-08-09", "-250.00", dining["id"])

        tree = client.get(
            "/api/reports/category-tree", params={"start": "2026-08-01", "end": "2026-08-31"}
        ).json()
        food_node = next(node for node in tree if node["category_id"] == food["id"])
        assert food_node["net"] == "-750.00"
        groceries_node = next(
            c for c in food_node["children"] if c["category_id"] == groceries_sub["id"]
        )
        assert groceries_node["net"] == "-500.00"

    def test_flat_categories_still_appear_as_top_level_nodes(
        self, client, checking, rent_category, make_txn
    ):
        make_txn(checking["id"], "2026-08-01", "-2000.00", rent_category["id"])
        tree = client.get(
            "/api/reports/category-tree", params={"start": "2026-08-01", "end": "2026-08-31"}
        ).json()
        rent = next(node for node in tree if node["category_id"] == rent_category["id"])
        assert rent["net"] == "-2000.00"
        assert rent["children"] == []

    def test_uncategorized_spending_still_shows_up(self, client, checking, make_txn):
        make_txn(checking["id"], "2026-08-01", "-15.00")
        tree = client.get(
            "/api/reports/category-tree", params={"start": "2026-08-01", "end": "2026-08-31"}
        ).json()
        assert any(node["name"] == "(uncategorized)" for node in tree)

    def test_flat_actuals_endpoint_unchanged(
        self, client, checking, groceries_sub, make_txn
    ):
        """The flat endpoint keeps working for callers that want leaf rows."""
        make_txn(checking["id"], "2026-08-02", "-600.00", groceries_sub["id"])
        flat = client.get(
            "/api/reports/category-actuals", params={"start": "2026-08-01", "end": "2026-08-31"}
        ).json()
        assert len(flat) == 1
        assert flat[0]["category_id"] == groceries_sub["id"]
