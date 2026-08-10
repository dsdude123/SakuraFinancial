"""Either-level budgeting: you may budget the parent ("Food") or the children
(Groceries / Dining Out), and spending always lands in exactly one envelope."""

FOOD, GROCERIES, DINING = 20, 21, 22


def row_for(view, category_id):
    return next(r for r in view["categories"] if r["category_id"] == category_id)


class TestBudgetAtParent:
    def test_child_spending_counts_against_the_parent_envelope(self, client, ledger):
        ledger.set_month("2026-08", income="5000.00", spend={GROCERIES: "600.00", DINING: "250.00"})
        client.put(f"/api/budget/2026-08/categories/{FOOD}", json={"amount": "800.00"})

        view = client.get("/api/budget/2026-08").json()
        food = row_for(view, FOOD)
        assert food["budgeted"] == "800.00"
        assert food["spent"] == "850.00"  # both children roll up
        assert food["available"] == "-50.00"
        assert food["over"] is True
        assert food["is_subtotal"] is False

    def test_children_still_listed_underneath_for_the_breakdown(self, client, ledger):
        ledger.set_month("2026-08", spend={GROCERIES: "600.00", DINING: "250.00"})
        client.put(f"/api/budget/2026-08/categories/{FOOD}", json={"amount": "800.00"})

        view = client.get("/api/budget/2026-08").json()
        assert row_for(view, GROCERIES)["spent"] == "600.00"
        assert row_for(view, DINING)["spent"] == "250.00"
        # Children have no envelope of their own: they report spending but must
        # never look "over budget" just because they have no budget line.
        assert row_for(view, GROCERIES)["is_envelope"] is False
        assert row_for(view, GROCERIES)["available"] is None
        assert row_for(view, GROCERIES)["over"] is False

    def test_children_are_indented_under_their_parent(self, client, ledger):
        ledger.set_month("2026-08", spend={GROCERIES: "600.00"})
        client.put(f"/api/budget/2026-08/categories/{FOOD}", json={"amount": "800.00"})
        view = client.get("/api/budget/2026-08").json()
        ids = [r["category_id"] for r in view["categories"]]
        depths = {r["category_id"]: r["depth"] for r in view["categories"]}
        assert ids.index(FOOD) < ids.index(GROCERIES)
        assert depths[FOOD] == 0 and depths[GROCERIES] == 1


class TestBudgetAtChildren:
    def test_child_budget_owns_its_own_spending(self, client, ledger):
        ledger.set_month("2026-08", spend={GROCERIES: "600.00", DINING: "250.00"})
        client.put(f"/api/budget/2026-08/categories/{GROCERIES}", json={"amount": "500.00"})
        client.put(f"/api/budget/2026-08/categories/{DINING}", json={"amount": "300.00"})

        view = client.get("/api/budget/2026-08").json()
        assert row_for(view, GROCERIES)["available"] == "-100.00"
        assert row_for(view, GROCERIES)["over"] is True
        assert row_for(view, DINING)["available"] == "50.00"

    def test_parent_becomes_a_read_only_subtotal(self, client, ledger):
        ledger.set_month("2026-08", spend={GROCERIES: "600.00", DINING: "250.00"})
        client.put(f"/api/budget/2026-08/categories/{GROCERIES}", json={"amount": "500.00"})
        client.put(f"/api/budget/2026-08/categories/{DINING}", json={"amount": "300.00"})

        food = row_for(client.get("/api/budget/2026-08").json(), FOOD)
        assert food["is_subtotal"] is True
        assert food["budgeted"] == "800.00"  # 500 + 300
        assert food["spent"] == "850.00"
        assert food["available"] == "-50.00"


class TestMixedLevels:
    def test_child_budget_wins_and_parent_covers_the_rest(self, client, ledger):
        """Groceries has its own envelope; Dining Out falls through to Food."""
        ledger.set_month("2026-08", spend={GROCERIES: "600.00", DINING: "250.00"})
        client.put(f"/api/budget/2026-08/categories/{FOOD}", json={"amount": "400.00"})
        client.put(f"/api/budget/2026-08/categories/{GROCERIES}", json={"amount": "500.00"})

        view = client.get("/api/budget/2026-08").json()
        groceries = row_for(view, GROCERIES)
        food = row_for(view, FOOD)
        assert groceries["spent"] == "600.00"
        assert groceries["available"] == "-100.00"
        # Food keeps its own envelope and is charged only Dining Out
        assert food["is_subtotal"] is False
        assert food["budgeted"] == "400.00"
        assert food["spent"] == "250.00"
        assert food["available"] == "150.00"

    def test_parents_direct_spending_stays_in_its_own_envelope(self, client, ledger):
        ledger.set_month("2026-08", spend={FOOD: "75.00", GROCERIES: "600.00"})
        client.put(f"/api/budget/2026-08/categories/{FOOD}", json={"amount": "800.00"})
        food = row_for(client.get("/api/budget/2026-08").json(), FOOD)
        assert food["spent"] == "675.00"  # its own 75 plus the child's 600


class TestDeficitsWithHierarchy:
    def test_parent_envelope_deficit_carries_forward(self, client, ledger):
        ledger.set_month("2026-07", income="5000.00", spend={GROCERIES: "900.00"})
        ledger.set_month("2026-08", income="5000.00", spend={GROCERIES: "700.00"})
        for month in ("2026-07", "2026-08"):
            client.put(f"/api/budget/{month}/categories/{FOOD}", json={"amount": "800.00"})

        august = row_for(client.get("/api/budget/2026-08").json(), FOOD)
        assert august["carry_in"] == "-100.00"  # July's overspend follows Food
        assert august["available"] == "0.00"  # healed by spending less

    def test_deficit_follows_the_envelope_not_the_category(self, client, ledger):
        """Overspend on a child lands on the parent's envelope, and the child
        never accumulates a carryover of its own."""
        ledger.set_month("2026-07", spend={DINING: "900.00"})
        ledger.set_month("2026-08", spend={})
        for month in ("2026-07", "2026-08"):
            client.put(f"/api/budget/{month}/categories/{FOOD}", json={"amount": "800.00"})

        view = client.get("/api/budget/2026-08").json()
        assert row_for(view, FOOD)["carry_in"] == "-100.00"
        assert all(
            row["carry_in"] == "0.00"
            for row in view["categories"]
            if row["category_id"] != FOOD
        )


class TestTotalsAreNotDoubleCounted:
    def test_month_totals_count_each_dollar_once(self, client, ledger):
        ledger.set_month("2026-08", income="5000.00", spend={GROCERIES: "600.00", DINING: "250.00"})
        client.put(f"/api/budget/2026-08/categories/{GROCERIES}", json={"amount": "500.00"})
        client.put(f"/api/budget/2026-08/categories/{DINING}", json={"amount": "300.00"})

        view = client.get("/api/budget/2026-08").json()
        # The Food subtotal row must not inflate the footer totals.
        assert view["total_budgeted"] == "800.00"
        assert view["total_spent"] == "850.00"
        assert view["waterfall"]["spent"] == "850.00"
