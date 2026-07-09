import tempfile
import unittest
from pathlib import Path

import award_watch


def make_item(origin="SFO", destination="NRT", date="2026-09-01", program="united",
              j_available=True, remaining=2, mileage="70000", airlines="NH"):
    return {
        "Route": {"OriginAirport": origin, "DestinationAirport": destination},
        "Date": date,
        "Source": program,
        "JAvailable": j_available,
        "JRemainingSeats": remaining,
        "JMileageCost": mileage,
        "JMileageCostRaw": int(mileage),
        "JAirlines": airlines,
        "JTotalTaxes": 12000,
        "TaxesCurrency": "JPY",
    }


class AwardWatchTests(unittest.TestCase):
    def base_config(self):
        return {
            "seats_aero": {
                "api_key": "test-key",
                "origins": ["SFO"],
                "destinations": ["NRT"],
                "carriers": ["NH", "JL", "UA", "AA"],
                "cabin": "business",
                "min_remaining_seats": 1,
            }
        }

    def test_build_hits_filters_unavailable_and_low_seats(self):
        config = self.base_config()
        config["seats_aero"]["min_remaining_seats"] = 2
        items = [
            make_item(j_available=True, remaining=2),
            make_item(j_available=False),
            make_item(j_available=True, remaining=1),  # 已知余位但低于阈值 -> 过滤
        ]
        hits = award_watch.build_hits(items, config)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].origin, "SFO")
        self.assertEqual(hits[0].remaining_seats, 2)

    def test_build_hits_filters_by_carrier(self):
        config = self.base_config()
        config["seats_aero"]["carriers"] = ["JL"]
        items = [make_item(airlines="NH")]
        hits = award_watch.build_hits(items, config)
        self.assertEqual(hits, [])

    def test_sync_availability_only_alerts_on_new_appearance(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = award_watch.init_db(Path(directory) / "test.sqlite3")
            hit = award_watch.build_hits([make_item()], self.base_config())[0]

            first_pass = award_watch.sync_availability(conn, [hit], "t1")
            second_pass = award_watch.sync_availability(conn, [hit], "t2")
            third_pass = award_watch.sync_availability(conn, [], "t3")
            fourth_pass = award_watch.sync_availability(conn, [hit], "t4")

            conn.close()

        self.assertEqual(len(first_pass), 1)
        self.assertEqual(len(second_pass), 0)
        self.assertEqual(len(third_pass), 0)
        self.assertEqual(len(fourth_pass), 1)

    def test_build_hits_keeps_unknown_seat_counts(self):
        # American 等计划 Available=true 但 RemainingSeats=0 表示"数量未知"，不能被过滤。
        config = self.base_config()
        items = [make_item(remaining=0, j_available=True)]
        hits = award_watch.build_hits(items, config)
        self.assertEqual(len(hits), 1)
        self.assertEqual(award_watch.format_seats(hits[0].remaining_seats), "未知")

    def partners(self):
        return {
            "currencies": {"amex_mr": "Amex MR", "chase_ur": "Chase UR", "citi_typ": "Citi TYP"},
            "programs": {
                "aeroplan": {
                    "display": "Air Canada Aeroplan",
                    "transfers": {
                        "amex_mr": {"ratio": 1.0, "time": "即时"},
                        "chase_ur": {"ratio": 1.0, "time": "即时"},
                    },
                },
                "velocity": {"display": "Virgin Australia Velocity", "transfers": {}, "note": "无美卡渠道"},
            },
        }

    def make_hit(self, program="aeroplan", cost=75000):
        return award_watch.AvailabilityHit(
            origin="SFO", destination="NRT", date="2026-09-01", cabin="business",
            program=program, mileage_cost=str(cost), mileage_cost_raw=cost,
            remaining_seats=2, airlines="NH", taxes=12000, taxes_currency="JPY",
        )

    def test_transfer_advice_sufficient_balance(self):
        wallet = {"points": {"amex_mr": 80000}, "airline_miles": {}}
        advice = award_watch.build_transfer_advice(self.make_hit(), wallet, self.partners())
        self.assertIn("✔", advice)
        self.assertIn("Amex MR", advice)
        self.assertIn("75,000", advice)

    def test_transfer_advice_uses_direct_miles_first(self):
        wallet = {"points": {}, "airline_miles": {"aeroplan": 80000}}
        advice = award_watch.build_transfer_advice(self.make_hit(), wallet, self.partners())
        self.assertIn("直接用", advice)

    def test_transfer_advice_insufficient_and_no_path(self):
        wallet = {"points": {"amex_mr": 10000}, "airline_miles": {}}
        advice = award_watch.build_transfer_advice(self.make_hit(), wallet, self.partners())
        self.assertIn("✘", advice)
        self.assertIn("65,000", advice)  # 还差 75000-10000

        advice2 = award_watch.build_transfer_advice(self.make_hit(program="velocity"), wallet, self.partners())
        self.assertIn("无美卡渠道", advice2)

    def test_transfer_advice_without_wallet_is_empty(self):
        advice = award_watch.build_transfer_advice(self.make_hit(), None, self.partners())
        self.assertEqual(advice, "")

    def test_generate_demo_items_pass_build_hits_filters(self):
        config = self.base_config()
        config["seats_aero"]["search_window_days"] = 60
        items = award_watch.generate_demo_items(config)
        hits = award_watch.build_hits(items, config)

        self.assertEqual(len(items), len(hits))
        for hit in hits:
            self.assertEqual(hit.origin, "SFO")
            self.assertEqual(hit.destination, "NRT")
            self.assertGreaterEqual(hit.remaining_seats, 1)

    def test_build_email_body_lists_all_hits(self):
        config = self.base_config()
        hits = award_watch.build_hits([make_item(), make_item(date="2026-09-02")], config)

        text_body, html_body = award_watch.build_email_body(hits)

        self.assertIn("SFO->NRT", text_body)
        self.assertIn("2026-09-01", text_body)
        self.assertIn("2026-09-02", text_body)
        self.assertIn("SFO", html_body)


if __name__ == "__main__":
    unittest.main()
