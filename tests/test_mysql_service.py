import unittest
from unittest.mock import patch

from modules.mysql_service import _requires_mysqlsh, list_rest_service_paths


class MysqlServiceTests(unittest.TestCase):
    def test_requires_mysqlsh_for_rest_commands(self) -> None:
        self.assertTrue(_requires_mysqlsh("SHOW REST SERVICES"))
        self.assertTrue(_requires_mysqlsh("SHOW CREATE REST SERVICE /app"))
        self.assertTrue(_requires_mysqlsh("CREATE OR REPLACE REST SERVICE /app PUBLISHED"))
        self.assertTrue(_requires_mysqlsh('ALTER REST SERVICE /app ADD AUTH APP "MySQL"'))
        self.assertTrue(_requires_mysqlsh("DROP REST SERVICE /app"))

    def test_requires_mysqlsh_for_mixed_rest_script(self) -> None:
        script = """
        CREATE DATABASE IF NOT EXISTS restapidb;
        CREATE OR REPLACE REST SCHEMA /restapidb ON SERVICE /app
            FROM `restapidb`
            ENABLED
            AUTHENTICATION REQUIRED;
        """
        self.assertTrue(_requires_mysqlsh(script))

    def test_connector_can_handle_normal_sql(self) -> None:
        self.assertFalse(_requires_mysqlsh("SHOW DATABASES"))
        self.assertFalse(_requires_mysqlsh("SHOW CREATE VIEW `restapidb`.`employees`"))
        self.assertFalse(_requires_mysqlsh("SELECT 'SHOW REST SERVICES' AS example"))

    def test_leading_comments_do_not_hide_rest_command(self) -> None:
        self.assertTrue(_requires_mysqlsh("-- generated\nSHOW REST SERVICES"))
        self.assertTrue(_requires_mysqlsh("/* generated */\nCREATE REST SERVICE /app"))

    def test_rest_service_paths_use_metadata_sql(self) -> None:
        captured_sql = []

        def fake_run_admin_sql(sql: str, *, raw_output: bool = False):
            captured_sql.append(sql)
            return [{"service_path": "/london"}, {"service_path": "/"}]

        with patch("modules.mysql_service.get_cached_value", return_value=None), patch(
            "modules.mysql_service.set_cached_value", side_effect=lambda _key, value: value
        ), patch("modules.mysql_service.run_admin_sql", side_effect=fake_run_admin_sql):
            self.assertEqual(list_rest_service_paths(), ["/london", "/"])

        self.assertEqual(len(captured_sql), 1)
        self.assertIn("mysql_rest_service_metadata.service", captured_sql[0])
        self.assertNotIn("SHOW REST", captured_sql[0].upper())


if __name__ == "__main__":
    unittest.main()
