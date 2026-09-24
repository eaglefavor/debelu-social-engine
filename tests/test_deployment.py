"""Tests for the deployment layer: URL normalisation, the process supervisor and
the libpq environment used for PostgreSQL backups.

These cover the failure modes that only show up on a managed host, where Docker
is not available to catch them: a provider-supplied ``postgresql://`` URL, a
root-owned volume, and query-string options such as ``sslmode``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest import mock

from sqlalchemy.engine import make_url

from test_support import TEST_DATA  # noqa: F401
from backend import drive_ops, supervisor
from backend.config import normalize_database_url, settings as base_settings


class DatabaseUrlNormalisationTests(unittest.TestCase):
    """Managed hosts hand out URLs that name a driver this project does not ship."""

    def test_render_and_heroku_schemes_are_retargeted(self):
        for raw in ("postgresql://u:p@host:5432/db", "postgres://u:p@host:5432/db"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_database_url(raw), "postgresql+psycopg://u:p@host:5432/db")

    def test_explicit_driver_is_left_alone(self):
        raw = "postgresql+psycopg://u:p@host/db"
        self.assertEqual(normalize_database_url(raw), raw)

    def test_non_postgres_urls_are_untouched(self):
        for raw in ("sqlite:///./data/debelu.db", "mysql://u:p@h/db"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_database_url(raw), raw)

    def test_normalised_url_still_resolves_to_the_postgresql_backend(self):
        url = make_url(normalize_database_url("postgresql://u:p@h:5432/d"))
        self.assertEqual(url.get_backend_name(), "postgresql")
        self.assertEqual(url.get_driver_name(), "psycopg")
        self.assertEqual((url.host, url.port, url.database), ("h", 5432, "d"))

    def test_query_options_survive_normalisation(self):
        url = make_url(normalize_database_url("postgresql://u:p@h/d?sslmode=require"))
        self.assertEqual(dict(url.query), {"sslmode": "require"})


class LibpqEnvironmentTests(unittest.TestCase):
    """pg_dump talks libpq, not SQLAlchemy, so the URL has to be translated."""

    def test_tcp_url_maps_to_pg_variables(self):
        environment = drive_ops.libpq_environment(make_url("postgresql+psycopg://alice:secret@db.example:5433/debelu"))
        self.assertEqual(environment["PGHOST"], "db.example")
        self.assertEqual(environment["PGPORT"], "5433")
        self.assertEqual(environment["PGUSER"], "alice")
        self.assertEqual(environment["PGPASSWORD"], "secret")
        self.assertEqual(environment["PGDATABASE"], "debelu")

    def test_socket_url_reads_host_from_the_query_string(self):
        """A Unix-socket URL has no netloc; host lives in the query string."""
        environment = drive_ops.libpq_environment(make_url("postgresql+psycopg://postgres:@/postgres?host=/tmp/pgdata"))
        self.assertEqual(environment["PGHOST"], "/tmp/pgdata", "must not silently fall back to localhost")
        self.assertEqual(environment["PGDATABASE"], "postgres")

    def test_ssl_and_other_options_become_pg_variables(self):
        """Managed providers commonly require sslmode; dropping it breaks pg_dump."""
        environment = drive_ops.libpq_environment(make_url(
            "postgresql+psycopg://u:p@h/d?sslmode=require&connect_timeout=17"))
        self.assertEqual(environment["PGSSLMODE"], "require")
        self.assertEqual(environment["PGCONNECT_TIMEOUT"], "17")

    def test_defaults_are_applied_when_absent(self):
        environment = drive_ops.libpq_environment(make_url("postgresql+psycopg:///db"))
        self.assertEqual(environment["PGHOST"], "localhost")
        self.assertEqual(environment["PGPORT"], "5432")
        self.assertEqual(environment["PGUSER"], "postgres")

    def test_password_is_carried_in_the_environment_not_the_command_line(self):
        """Credentials must never reach argv, where they are visible in ps."""
        captured = {}

        def fake_run(command, *args, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs.get("env", {})
            # pg_dump writes the dump itself; reproduce that so the size check works.
            Path(command[command.index("--file") + 1]).write_bytes(b"PGDMP-fake")
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(drive_ops.shutil, "which", return_value="/usr/bin/pg_dump"), \
                 mock.patch.object(drive_ops.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(drive_ops, "sqlite_path", return_value=None), \
                 mock.patch.object(drive_ops, "database_url_parts",
                                   return_value=make_url("postgresql+psycopg://alice:topsecret@h:5432/d")):
                drive_ops.snapshot_database(Path(directory) / "out.dump")
        self.assertEqual(captured["env"]["PGPASSWORD"], "topsecret")
        self.assertNotIn("topsecret", " ".join(captured["command"]))

    def test_missing_pg_dump_is_reported_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(drive_ops.shutil, "which", return_value=None), \
                 mock.patch.object(drive_ops, "database_url_parts",
                                   return_value=make_url("postgresql+psycopg://u:p@h/d")):
                with self.assertRaises(drive_ops.DriveError) as caught:
                    drive_ops.snapshot_database(Path(directory) / "out.dump")
        self.assertIn("pg_dump is not on PATH", str(caught.exception))


class SupervisorTests(unittest.TestCase):
    """Dispatch tests.

    These must never spawn a real server or worker. Mocking ``os.execvp`` alone is
    not enough: while ``main`` had no early return, a mocked exec fell through into
    the supervise branch, which started both processes for real. Locally that
    merely failed to bind port 8000, so the suite stayed green; on CI the port is
    free, uvicorn served forever and the job blocked until it timed out. The
    ``supervise`` patcher below turns that class of mistake into a loud failure.
    """

    def setUp(self):
        # A developer's own .env must not choose the branch a test exercises.
        self.load_environment = mock.patch.object(supervisor, "load_environment")
        self.load_environment.start()
        self.addCleanup(self.load_environment.stop)
        self.supervise = mock.patch.object(
            supervisor, "supervise",
            side_effect=AssertionError("a single-process mode must never call supervise()"))
        self.supervise_mock = self.supervise.start()
        self.addCleanup(self.supervise.stop)

    def test_run_mode_defaults_to_web_and_rejects_unknown_values(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUN_MODE", None)
            self.assertEqual(supervisor.run_mode(), "web")
        with mock.patch.dict(os.environ, {"RUN_MODE": "all"}):
            self.assertEqual(supervisor.run_mode(), "all")
        with mock.patch.dict(os.environ, {"RUN_MODE": "nonsense"}):
            with self.assertRaises(SystemExit) as caught:
                supervisor.run_mode()
        self.assertIn("web, worker", str(caught.exception))

    def test_port_defaults_to_8000_and_honours_the_platform(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORT", None)
            self.assertEqual(supervisor.port(), "8000")
        with mock.patch.dict(os.environ, {"PORT": "10000"}):
            self.assertEqual(supervisor.port(), "10000")
        # A blank PORT must not produce "--port ''".
        with mock.patch.dict(os.environ, {"PORT": "   "}):
            self.assertEqual(supervisor.port(), "8000")

    def test_web_command_binds_all_interfaces_on_the_platform_port(self):
        with mock.patch.dict(os.environ, {"PORT": "10000"}):
            command = supervisor.web_command()
        self.assertIn("backend.main:app", command)
        self.assertEqual(command[command.index("--host") + 1], "0.0.0.0")
        self.assertEqual(command[command.index("--port") + 1], "10000")
        self.assertIn("--proxy-headers", command)

    def test_worker_command_runs_the_module(self):
        self.assertEqual(supervisor.worker_command()[-2:], ["-m", "backend.worker"])

    def test_failed_migrations_stop_the_container(self):
        with mock.patch.object(supervisor.subprocess, "run", return_value=mock.Mock(returncode=2)):
            with self.assertRaises(SystemExit) as caught:
                supervisor.migrate()
        self.assertIn("refusing to start", str(caught.exception))

    def test_successful_migrations_do_not_raise(self):
        with mock.patch.object(supervisor.subprocess, "run", return_value=mock.Mock(returncode=0)):
            supervisor.migrate()

    def test_worker_mode_does_not_run_migrations(self):
        with mock.patch.dict(os.environ, {"RUN_MODE": "worker"}), \
             mock.patch.object(supervisor, "ensure_writable_directories"), \
             mock.patch.object(supervisor, "migrate") as migrate, \
             mock.patch.object(supervisor.os, "execvp") as execvp:
            with self.assertRaises(SystemExit) as caught:
                supervisor.main()
        migrate.assert_not_called()
        self.assertIn("os.execvp returned", str(caught.exception))
        self.assertEqual(execvp.call_args[0][1][1:], ["-m", "backend.worker"])

    def test_web_mode_migrates_before_exec_ing(self):
        calls: list[str] = []
        with mock.patch.dict(os.environ, {"RUN_MODE": "web"}), \
             mock.patch.object(supervisor, "ensure_writable_directories"), \
             mock.patch.object(supervisor, "migrate", side_effect=lambda: calls.append("migrate")), \
             mock.patch.object(supervisor.os, "execvp") as execvp:
            with self.assertRaises(SystemExit):
                supervisor.main()
        self.assertEqual(calls, ["migrate"], "migrations must run before the server starts")
        self.assertTrue(execvp.called, "web mode should replace the process image")
        self.assertIn("uvicorn", execvp.call_args[0][1])

    def test_a_mocked_exec_still_does_not_start_the_second_process(self):
        """Regression test for the CI hang: no fall-through into the 'all' branch."""
        with mock.patch.dict(os.environ, {"RUN_MODE": "web"}), \
             mock.patch.object(supervisor, "ensure_writable_directories"), \
             mock.patch.object(supervisor, "migrate"), \
             mock.patch.object(supervisor.os, "execvp"):
            with self.assertRaises(SystemExit):
                supervisor.main()
        self.supervise_mock.assert_not_called()

    def test_all_mode_supervises_both_processes(self):
        with mock.patch.dict(os.environ, {"RUN_MODE": "all"}), \
             mock.patch.object(supervisor, "ensure_writable_directories"), \
             mock.patch.object(supervisor, "migrate") as migrate, \
             mock.patch.object(supervisor, "supervise", return_value=0) as supervise:
            self.assertEqual(supervisor.main(), 0)
        migrate.assert_called_once()
        supervise.assert_called_once()

    def test_environment_is_loaded_before_the_mode_is_chosen(self):
        order: list[str] = []
        with mock.patch.dict(os.environ, {"RUN_MODE": "worker"}), \
             mock.patch.object(supervisor, "load_environment", side_effect=lambda: order.append("load_environment")), \
             mock.patch.object(supervisor, "run_mode", side_effect=lambda: order.append("run_mode") or "worker"), \
             mock.patch.object(supervisor, "ensure_writable_directories"), \
             mock.patch.object(supervisor, "migrate"), \
             mock.patch.object(supervisor.os, "execvp"):
            with self.assertRaises(SystemExit):
                supervisor.main()
        self.assertEqual(order, ["load_environment", "run_mode"], "env must load before RUN_MODE is read")


class WritableDirectoryGuardTests(unittest.TestCase):
    def test_missing_directories_are_created(self):
        with tempfile.TemporaryDirectory() as directory:
            target = replace(base_settings,
                             media_dir=Path(directory) / "media",
                             staging_dir=Path(directory) / "staging")
            with mock.patch("backend.config.settings", target):
                supervisor.ensure_writable_directories()
            self.assertTrue((Path(directory) / "media").is_dir())
            self.assertTrue((Path(directory) / "staging").is_dir())

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_unwritable_directory_fails_with_actionable_guidance(self):
        """The most likely managed-host failure: a root-owned mounted volume."""
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "media"
            blocked.mkdir()
            blocked.chmod(0o500)
            target = replace(base_settings, media_dir=blocked, staging_dir=Path(directory) / "staging")
            try:
                with mock.patch("backend.config.settings", target):
                    with self.assertRaises(SystemExit) as caught:
                        supervisor.ensure_writable_directories()
            finally:
                blocked.chmod(0o700)
            message = str(caught.exception)
            self.assertIn("MEDIA_DIR", message)
            self.assertIn("chown -R 10001:10001", message, "must tell the operator how to fix it")

    def test_no_probe_files_are_left_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            target = replace(base_settings, media_dir=media, staging_dir=Path(directory) / "staging")
            with mock.patch("backend.config.settings", target):
                supervisor.ensure_writable_directories()
            self.assertEqual(list(media.glob(".write-probe-*")), [])


if __name__ == "__main__":
    unittest.main()


class EnvironmentLoadingTests(unittest.TestCase):
    def test_load_environment_reads_a_dotenv_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("DEBELU_SUPERVISOR_PROBE=loaded\n", encoding="utf-8")
            os.environ.pop("DEBELU_SUPERVISOR_PROBE", None)
            try:
                from backend.config import load_dotenv
                load_dotenv(env_file)
                self.assertEqual(os.environ.get("DEBELU_SUPERVISOR_PROBE"), "loaded")
            finally:
                os.environ.pop("DEBELU_SUPERVISOR_PROBE", None)
