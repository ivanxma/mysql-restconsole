import unittest
from unittest.mock import patch

from modules.mysql_service import (
    _requires_mrs_sql_extensions,
    create_rest_procedure_definition,
    create_rest_service_definition,
    create_rest_service_path_definition,
    expose_database_to_service_definition,
    expose_existing_schema_procedure,
    list_rest_service_paths,
    run_admin_ddl,
)


class MysqlServiceTests(unittest.TestCase):
    def test_requires_mrs_sql_extensions_for_rest_commands(self) -> None:
        self.assertTrue(_requires_mrs_sql_extensions("SHOW REST SERVICES"))
        self.assertTrue(_requires_mrs_sql_extensions("SHOW CREATE REST SERVICE /app"))
        self.assertTrue(_requires_mrs_sql_extensions("CREATE OR REPLACE REST SERVICE /app PUBLISHED"))
        self.assertTrue(_requires_mrs_sql_extensions('ALTER REST SERVICE /app ADD AUTH APP "MySQL"'))
        self.assertTrue(_requires_mrs_sql_extensions("DROP REST SERVICE /app"))

    def test_requires_mrs_sql_extensions_for_mixed_rest_script(self) -> None:
        script = """
        CREATE DATABASE IF NOT EXISTS restapidb;
        CREATE OR REPLACE REST SCHEMA /restapidb ON SERVICE /app
            FROM `restapidb`
            ENABLED
            AUTHENTICATION REQUIRED;
        """
        self.assertTrue(_requires_mrs_sql_extensions(script))

    def test_connector_can_handle_normal_sql(self) -> None:
        self.assertFalse(_requires_mrs_sql_extensions("SHOW DATABASES"))
        self.assertFalse(_requires_mrs_sql_extensions("SHOW CREATE VIEW `restapidb`.`employees`"))
        self.assertFalse(_requires_mrs_sql_extensions("SELECT 'SHOW REST SERVICES' AS example"))

    def test_leading_comments_do_not_hide_rest_command(self) -> None:
        self.assertTrue(_requires_mrs_sql_extensions("-- generated\nSHOW REST SERVICES"))
        self.assertTrue(_requires_mrs_sql_extensions("/* generated */\nCREATE REST SERVICE /app"))

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

    def test_rest_ddl_is_rejected_without_subprocess(self) -> None:
        with patch("modules.mysql_service._run_mrs_sql_extensions", return_value=""):
            run_admin_ddl("CREATE OR REPLACE REST SERVICE /london PUBLISHED")

    def test_create_rest_service_action_uses_mrs_extensions(self) -> None:
        with patch("modules.mysql_service._run_mrs_sql_extensions", return_value="") as runner:
            create_rest_service_path_definition(service_name="London")
        self.assertIn("CREATE OR REPLACE REST SERVICE", runner.call_args.args[0])

    def test_expose_database_action_uses_mrs_extensions(self) -> None:
        with patch("modules.mysql_service._run_mrs_sql_extensions", return_value="") as runner:
            expose_database_to_service_definition(
                service_path="/london",
                source_schema="employees",
                auth_required=False,
            )
        self.assertIn("CREATE OR REPLACE REST SCHEMA", runner.call_args.args[0])

    def test_expose_table_action_uses_mrs_extensions(self) -> None:
        with patch(
            "modules.mysql_service.list_table_columns",
            return_value=[{"column_name": "id", "column_key": "PRI"}],
        ), patch("modules.mysql_service._run_mrs_sql_extensions", return_value="") as runner:
            create_rest_service_definition(
                service_path="/london",
                source_schema="employees",
                source_table="departments",
                auth_required=False,
            )
        self.assertIn("CREATE OR REPLACE REST VIEW", runner.call_args.args[0])

    def test_create_rest_procedure_action_uses_mrs_extensions(self) -> None:
        with patch("modules.mysql_service._run_mrs_sql_extensions", return_value="") as runner:
            create_rest_procedure_definition(
                procedure_name="department_lookup",
                service_path="/london",
                auth_required=False,
                parameters=[],
                body_sql="SELECT 1",
            )
        self.assertIn("CREATE OR REPLACE REST PROCEDURE", runner.call_args.args[0])

    def test_expose_sys_procedure_action_uses_mrs_extensions(self) -> None:
        with patch("modules.mysql_service.list_procedure_parameters", return_value=[]), patch(
            "modules.mysql_service._run_mrs_sql_extensions", return_value=""
        ) as runner:
            expose_existing_schema_procedure(
                source_schema="sys",
                procedure_name="ps_setup_enable_thread",
                service_path="/london",
                auth_required=False,
            )
        self.assertIn("CREATE OR REPLACE REST PROCEDURE", runner.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
