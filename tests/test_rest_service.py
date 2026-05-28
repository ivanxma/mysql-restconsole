import unittest
from unittest.mock import patch

from modules.rest_service import get_rest_procedure_details, get_rest_service_auth_details, normalize_rest_response


class RestServiceTests(unittest.TestCase):
    def test_non_procedure_items_response_becomes_result_rows(self) -> None:
        response = normalize_rest_response(
            {
                "items": [
                    {"id": 1, "name": "London"},
                    {"id": 2, "name": "Tokyo"},
                ],
                "limit": 25,
            },
            object_kind="VIEW",
        )

        self.assertEqual(response["result_sets"][0]["name"], "Items")
        self.assertEqual(
            response["result_sets"][0]["rows"],
            [
                {"id": 1, "name": "London"},
                {"id": 2, "name": "Tokyo"},
            ],
        )

    def test_auth_details_use_metadata_sql(self) -> None:
        captured_sql = []

        def fake_run_admin_sql(sql: str, *, raw_output: bool = False):
            captured_sql.append(sql)
            return [{"auth_path": "/authentication", "auth_required": "Required"}]

        with patch("modules.rest_service.get_cached_value", return_value=None), patch(
            "modules.rest_service.set_cached_value", side_effect=lambda _key, value: value
        ), patch("modules.rest_service.run_admin_sql", side_effect=fake_run_admin_sql):
            details = get_rest_service_auth_details("/london", schema_path="/restapidb", auth_apps="MySQL")

        self.assertEqual(details["auth_required"], "Required")
        self.assertEqual(details["auth_path"], "/authentication")
        self.assertEqual(len(captured_sql), 1)
        self.assertIn("mysql_rest_service_metadata.service", captured_sql[0])
        self.assertNotIn("SHOW CREATE REST", captured_sql[0].upper())

    def test_procedure_details_use_metadata_and_information_schema(self) -> None:
        captured_sql = []

        def fake_run_admin_sql(sql: str, *, raw_output: bool = False):
            captured_sql.append(sql)
            if "information_schema.parameters" in sql:
                return [{"parameter_mode": "IN", "parameter_name": "employee_id"}]
            return [{"routine_schema": "restapidb", "routine_name": "employee_lookup"}]

        with patch("modules.rest_service.get_cached_value", return_value=None), patch(
            "modules.rest_service.set_cached_value", side_effect=lambda _key, value: value
        ), patch("modules.rest_service.run_admin_sql", side_effect=fake_run_admin_sql):
            details = get_rest_procedure_details(
                service_path="/london",
                schema_path="/restapidb",
                object_path="/employee_lookup",
            )

        self.assertEqual(
            details["procedure_params"],
            [{"name": "employee_id", "source_name": "employee_id", "mode": "IN"}],
        )
        self.assertEqual(len(captured_sql), 2)
        self.assertIn("mysql_rest_service_metadata.service", captured_sql[0])
        self.assertIn("information_schema.parameters", captured_sql[1])
        self.assertTrue(all("SHOW CREATE REST" not in sql.upper() for sql in captured_sql))


if __name__ == "__main__":
    unittest.main()
