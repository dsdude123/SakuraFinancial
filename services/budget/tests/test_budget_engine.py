class TestMonthView:
    def test_budgeted_vs_spent(self, client, ledger):
        ledger.set_month("2026-08", income="5000.00", spend={1: "2000.00", 2: "350.00"})
        client.put("/api/budget/2026-08/categories/1", json={"amount": "2000.00"})
        client.put("/api/budget/2026-08/categories/2", json={"amount": "400.00"})

        view = client.get("/api/budget/2026-08").json()
        rent = next(r for r in view["categories"] if r["category_id"] == 1)
        groceries = next(r for r in view["categories"] if r["category_id"] == 2)
        assert rent["available"] == "0.00"
        assert groceries["available"] == "50.00"
        assert view["income"] == "5000.00"
        assert view["total_spent"] == "2350.00"

    def test_bad_month_format(self, client):
        assert client.get("/api/budget/aug-2026").status_code == 422

    def test_spending_without_budget_still_listed(self, client, ledger):
        ledger.set_month("2026-08", spend={2: "120.00"})
        view = client.get("/api/budget/2026-08").json()
        groceries = next(r for r in view["categories"] if r["category_id"] == 2)
        assert groceries["budgeted"] == "0.00"
        assert groceries["spent"] == "120.00"
        assert groceries["over"] is True


class TestDeficitCarryover:
    def test_overspend_reduces_next_month(self, client, ledger):
        """YNAB's weak deficit story, fixed: July's overspend follows you."""
        ledger.set_month("2026-07", income="5000.00", spend={2: "500.00"})
        ledger.set_month("2026-08", income="5000.00", spend={2: "300.00"})
        client.put("/api/budget/2026-07/categories/2", json={"amount": "400.00"})
        client.put("/api/budget/2026-08/categories/2", json={"amount": "400.00"})

        july = client.get("/api/budget/2026-07").json()
        july_groceries = next(r for r in july["categories"] if r["category_id"] == 2)
        assert july_groceries["available"] == "-100.00"
        assert july_groceries["over"] is True

        august = client.get("/api/budget/2026-08").json()
        august_groceries = next(r for r in august["categories"] if r["category_id"] == 2)
        assert august_groceries["carry_in"] == "-100.00"
        # 400 budget - 100 deficit - 300 spent = 0: recovered by spending less
        assert august_groceries["available"] == "0.00"
        assert august_groceries["over"] is False

    def test_deficit_persists_until_recovered(self, client, ledger):
        ledger.set_month("2026-07", spend={2: "500.00"})
        ledger.set_month("2026-08", spend={2: "400.00"})
        ledger.set_month("2026-09", spend={2: "300.00"})
        for month in ("2026-07", "2026-08", "2026-09"):
            client.put(f"/api/budget/{month}/categories/2", json={"amount": "400.00"})

        september = client.get("/api/budget/2026-09").json()
        groceries = next(r for r in september["categories"] if r["category_id"] == 2)
        # July -100 carried through August (400-100-400=-100), healed in September
        assert groceries["carry_in"] == "-100.00"
        assert groceries["available"] == "0.00"

    def test_surplus_does_NOT_roll_into_category(self, client, ledger):
        """Leftovers are General Fund money, not category padding."""
        ledger.set_month("2026-07", income="1000.00", spend={2: "300.00"})
        ledger.set_month("2026-08", spend={2: "0.00"})
        client.put("/api/budget/2026-07/categories/2", json={"amount": "400.00"})
        client.put("/api/budget/2026-08/categories/2", json={"amount": "400.00"})

        august = client.get("/api/budget/2026-08").json()
        groceries = next(r for r in august["categories"] if r["category_id"] == 2)
        assert groceries["carry_in"] == "0.00"
        # July's 700 leftover (1000 income - 300 spent) went to the General Fund
        assert august["waterfall"]["general_fund_total"] == "700.00"


class TestWaterfall:
    def test_remainder_lands_in_general_fund_without_nagging(self, client, ledger):
        ledger.set_month("2026-08", income="5000.00", spend={1: "2000.00"})
        view = client.get("/api/budget/2026-08").json()
        assert view["waterfall"]["spent"] == "2000.00"
        assert view["waterfall"]["general_fund_delta"] == "3000.00"
        assert view["global_deficit"] is False

    def test_bill_set_aside_tier(self, client, ledger):
        ledger.set_month("2026-08", income="5000.00", spend={1: "2000.00"})
        ledger.accrual = {
            "bills": [
                # annual insurance, due in 4 months: accrues $100/month
                {"monthly_load": "100.00", "months_until_due": 4},
                # monthly bill: months_until_due None -> tier 1 territory, skipped
                {"monthly_load": "142.19", "months_until_due": None},
                # annual bill due THIS month: its payment is real spending, skipped
                {"monthly_load": "50.00", "months_until_due": 0},
            ]
        }
        view = client.get("/api/budget/2026-08").json()
        assert view["waterfall"]["bill_set_aside"] == "100.00"
        assert view["waterfall"]["general_fund_delta"] == "2900.00"

    def test_goals_funded_in_priority_order_and_capped(self, client, ledger):
        ledger.set_month("2026-08", income="1000.00", spend={})
        client.post(
            "/api/goals",
            json={
                "name": "Taiwan trip",
                "target_amount": "300.00",
                "monthly_contribution": "500.00",
                "priority": 1,
            },
        )
        client.post(
            "/api/goals",
            json={
                "name": "New PC",
                "target_amount": "2000.00",
                "monthly_contribution": "400.00",
                "priority": 2,
            },
        )
        view = client.get("/api/budget/2026-08").json()
        goals = view["waterfall"]["goals"]
        # first goal capped at its 300 target despite 500/month pace
        assert goals[0]["allocated_this_month"] == "300.00"
        assert goals[0]["progress_pct"] == 100
        assert goals[1]["allocated_this_month"] == "400.00"
        assert view["waterfall"]["general_fund_delta"] == "300.00"

    def test_insufficient_income_starves_lower_tiers(self, client, ledger):
        ledger.set_month("2026-08", income="1000.00", spend={1: "900.00"})
        client.post(
            "/api/goals",
            json={"name": "G", "target_amount": "5000.00", "monthly_contribution": "400.00"},
        )
        view = client.get("/api/budget/2026-08").json()
        goals = view["waterfall"]["goals"]
        # only 100 left after spending; goal gets it all, general fund gets none
        assert goals[0]["allocated_this_month"] == "100.00"
        assert view["waterfall"]["general_fund_delta"] == "0.00"

    def test_overspending_month_goes_globally_negative(self, client, ledger):
        ledger.set_month("2026-08", income="1000.00", spend={1: "1500.00"})
        view = client.get("/api/budget/2026-08").json()
        assert view["waterfall"]["general_fund_delta"] == "-500.00"
        assert view["global_deficit"] is True

    def test_goal_funding_accumulates_across_months(self, client, ledger):
        ledger.set_month("2026-07", income="1000.00", spend={})
        ledger.set_month("2026-08", income="1000.00", spend={})
        client.put("/api/budget/2026-07/categories/2", json={"amount": "1.00"})  # anchor July
        client.post(
            "/api/goals",
            json={"name": "G", "target_amount": "5000.00", "monthly_contribution": "400.00"},
        )
        view = client.get("/api/budget/2026-08").json()
        goal = view["waterfall"]["goals"][0]
        assert goal["allocated_this_month"] == "400.00"
        assert goal["funded_total"] == "800.00"
        assert goal["progress_pct"] == 16


class TestBudgetsCrud:
    def test_zero_amount_deletes(self, client, ledger):
        client.put("/api/budget/2026-08/categories/2", json={"amount": "400.00"})
        client.put("/api/budget/2026-08/categories/2", json={"amount": "0"})
        view = client.get("/api/budget/2026-08").json()
        assert all(r["category_id"] != 2 for r in view["categories"])

    def test_negative_rejected(self, client):
        response = client.put("/api/budget/2026-08/categories/2", json={"amount": "-5"})
        assert response.status_code == 422

    def test_copy_from_previous(self, client, ledger):
        client.put("/api/budget/2026-07/categories/1", json={"amount": "2000.00"})
        client.put("/api/budget/2026-07/categories/2", json={"amount": "400.00"})
        result = client.post("/api/budget/2026-08/copy-from-previous").json()
        assert result["copied"] == 2
        view = client.get("/api/budget/2026-08").json()
        rent = next(r for r in view["categories"] if r["category_id"] == 1)
        assert rent["budgeted"] == "2000.00"

    def test_copy_with_nothing_to_copy_404(self, client):
        assert client.post("/api/budget/2026-08/copy-from-previous").status_code == 404


class TestGoalsCrud:
    def test_create_update_delete(self, client):
        goal = client.post(
            "/api/goals",
            json={"name": "G", "target_amount": "100.00", "monthly_contribution": "10.00"},
        ).json()
        client.put(f"/api/goals/{goal['id']}", json={"name": "Renamed"})
        assert client.get("/api/goals").json()[0]["name"] == "Renamed"
        client.delete(f"/api/goals/{goal['id']}")
        assert client.get("/api/goals").json() == []

    def test_invalid_amounts_rejected(self, client):
        response = client.post(
            "/api/goals",
            json={"name": "G", "target_amount": "0", "monthly_contribution": "10.00"},
        )
        assert response.status_code == 422


class TestExportImport:
    def test_round_trip(self, client, ledger):
        client.put("/api/budget/2026-08/categories/1", json={"amount": "2000.00"})
        client.post(
            "/api/goals",
            json={"name": "G", "target_amount": "100.00", "monthly_contribution": "10.00"},
        )
        exported = client.get("/api/export").json()
        assert exported["service"] == "budget"
        response = client.post("/api/import", json=exported)
        assert response.status_code == 200
        assert client.get("/api/goals").json()[0]["name"] == "G"
        view = client.get("/api/budget/2026-08").json()
        assert view["total_budgeted"] == "2000.00"
