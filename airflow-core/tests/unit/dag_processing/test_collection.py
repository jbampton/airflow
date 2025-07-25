#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import annotations

import logging
import warnings
from collections.abc import Generator
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest import mock
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, SAWarning

import airflow.dag_processing.collection
from airflow._shared.timezones import timezone as tz
from airflow.configuration import conf
from airflow.dag_processing.collection import (
    AssetModelOperation,
    DagModelOperation,
    _get_latest_runs_stmt,
    update_dag_parsing_results_in_db,
)
from airflow.exceptions import SerializationError
from airflow.listeners.listener import get_listener_manager
from airflow.models import DagModel, DagRun, Trigger
from airflow.models.asset import (
    AssetActive,
    AssetModel,
    DagScheduleAssetNameReference,
    DagScheduleAssetUriReference,
)
from airflow.models.dag import DAG
from airflow.models.errors import ParseImportError
from airflow.models.serialized_dag import SerializedDagModel
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.providers.standard.triggers.temporal import TimeDeltaTrigger
from airflow.sdk.definitions.asset import Asset, AssetAlias, AssetWatcher
from airflow.serialization.serialized_objects import LazyDeserializedDAG, SerializedDAG

from tests_common.test_utils.db import (
    clear_db_assets,
    clear_db_dags,
    clear_db_import_errors,
    clear_db_serialized_dags,
    clear_db_triggers,
)

if TYPE_CHECKING:
    from kgb import SpyAgency


def test_statement_latest_runs_one_dag():
    with warnings.catch_warnings():
        warnings.simplefilter("error", category=SAWarning)

        stmt = _get_latest_runs_stmt(["fake-dag"])
        compiled_stmt = str(stmt.compile())
        actual = [x.strip() for x in compiled_stmt.splitlines()]
        expected = [
            "SELECT dag_run.id, dag_run.dag_id, dag_run.logical_date, "
            "dag_run.data_interval_start, dag_run.data_interval_end",
            "FROM dag_run",
            "WHERE dag_run.dag_id = :dag_id_1 AND dag_run.logical_date = ("
            "SELECT max(dag_run.logical_date) AS max_logical_date",
            "FROM dag_run",
            "WHERE dag_run.dag_id = :dag_id_2 AND dag_run.run_type IN (__[POSTCOMPILE_run_type_1]))",
        ]
        assert actual == expected, compiled_stmt


def test_statement_latest_runs_many_dag():
    with warnings.catch_warnings():
        warnings.simplefilter("error", category=SAWarning)

        stmt = _get_latest_runs_stmt(["fake-dag-1", "fake-dag-2"])
        compiled_stmt = str(stmt.compile())
        actual = [x.strip() for x in compiled_stmt.splitlines()]
        expected = [
            "SELECT dag_run.id, dag_run.dag_id, dag_run.logical_date, "
            "dag_run.data_interval_start, dag_run.data_interval_end",
            "FROM dag_run, (SELECT dag_run.dag_id AS dag_id, max(dag_run.logical_date) AS max_logical_date",
            "FROM dag_run",
            "WHERE dag_run.dag_id IN (__[POSTCOMPILE_dag_id_1]) "
            "AND dag_run.run_type IN (__[POSTCOMPILE_run_type_1]) GROUP BY dag_run.dag_id) AS anon_1",
            "WHERE dag_run.dag_id = anon_1.dag_id AND dag_run.logical_date = anon_1.max_logical_date",
        ]
        assert actual == expected, compiled_stmt


@pytest.mark.db_test
class TestAssetModelOperation:
    @staticmethod
    def clean_db():
        clear_db_dags()
        clear_db_assets()
        clear_db_triggers()

    @pytest.fixture(autouse=True)
    def per_test(self) -> Generator:
        self.clean_db()
        yield
        self.clean_db()

    @pytest.mark.parametrize(
        "is_active, is_paused, expected_num_triggers",
        [
            (True, True, 0),
            (True, False, 1),
            (False, True, 0),
            (False, False, 0),
        ],
    )
    @pytest.mark.usefixtures("testing_dag_bundle")
    def test_add_asset_trigger_references(self, session, is_active, is_paused, expected_num_triggers):
        classpath, kwargs = TimeDeltaTrigger(timedelta(seconds=0)).serialize()
        asset = Asset(
            "test_add_asset_trigger_references_asset",
            watchers=[AssetWatcher(name="test", trigger={"classpath": classpath, "kwargs": kwargs})],
        )

        with DAG(dag_id="test_add_asset_trigger_references_dag", schedule=[asset]) as dag:
            EmptyOperator(task_id="mytask")

        dags = {dag.dag_id: dag}
        orm_dags = DagModelOperation(dags, "testing", None).add_dags(session=session)

        # Simulate dag unpause and deletion.
        dag_model = orm_dags[dag.dag_id]
        dag_model.is_stale = not is_active
        dag_model.is_paused = is_paused

        asset_op = AssetModelOperation.collect(dags)
        orm_assets = asset_op.sync_assets(session=session)
        session.flush()

        asset_op.add_dag_asset_references(orm_dags, orm_assets, session=session)
        asset_op.activate_assets_if_possible(orm_assets.values(), session=session)
        asset_op.add_asset_trigger_references(orm_assets, session=session)
        session.flush()

        asset_model = session.scalars(select(AssetModel)).one()
        assert len(asset_model.triggers) == expected_num_triggers
        assert session.scalar(select(func.count()).select_from(Trigger)) == expected_num_triggers

    @pytest.mark.parametrize(
        "schedule, model, columns, expected",
        [
            pytest.param(
                Asset.ref(name="name1"),
                DagScheduleAssetNameReference,
                (DagScheduleAssetNameReference.name, DagScheduleAssetNameReference.dag_id),
                [("name1", "test")],
                id="name-ref",
            ),
            pytest.param(
                Asset.ref(uri="foo://1"),
                DagScheduleAssetUriReference,
                (DagScheduleAssetUriReference.uri, DagScheduleAssetUriReference.dag_id),
                [("foo://1", "test")],
                id="uri-ref",
            ),
        ],
    )
    def test_add_dag_asset_name_uri_references(self, dag_maker, session, schedule, model, columns, expected):
        with dag_maker(dag_id="test", schedule=schedule, session=session) as dag:
            pass

        op = AssetModelOperation.collect({dag.dag_id: dag})
        op.add_dag_asset_name_uri_references(session=session)
        assert session.execute(select(*columns)).all() == expected

    def test_change_asset_property_sync_group(self, dag_maker, session):
        asset = Asset("myasset", group="old_group")
        with dag_maker(schedule=[asset]) as dag:
            EmptyOperator(task_id="mytask")

        asset_op = AssetModelOperation.collect({dag.dag_id: dag})
        orm_assets = asset_op.sync_assets(session=session)
        assert len(orm_assets) == 1
        assert next(iter(orm_assets.values())).group == "old_group"

        # Parser should pick up group change.
        asset.group = "new_group"
        asset_op = AssetModelOperation.collect({dag.dag_id: dag})
        orm_assets = asset_op.sync_assets(session=session)
        assert len(orm_assets) == 1
        assert next(iter(orm_assets.values())).group == "new_group"

    def test_change_asset_property_sync_extra(self, dag_maker, session):
        asset = Asset("myasset", extra={"foo": "old"})
        with dag_maker(schedule=asset) as dag:
            EmptyOperator(task_id="mytask")

        asset_op = AssetModelOperation.collect({dag.dag_id: dag})
        orm_assets = asset_op.sync_assets(session=session)
        assert len(orm_assets) == 1
        assert next(iter(orm_assets.values())).extra == {"foo": "old"}

        # Parser should pick up extra change.
        asset.extra = {"foo": "new"}
        asset_op = AssetModelOperation.collect({dag.dag_id: dag})
        orm_assets = asset_op.sync_assets(session=session)
        assert len(orm_assets) == 1
        assert next(iter(orm_assets.values())).extra == {"foo": "new"}

    def test_change_asset_alias_property_sync_group(self, dag_maker, session):
        alias = AssetAlias("myalias", group="old_group")
        with dag_maker(schedule=alias) as dag:
            EmptyOperator(task_id="mytask")

        asset_op = AssetModelOperation.collect({dag.dag_id: dag})
        orm_aliases = asset_op.sync_asset_aliases(session=session)
        assert len(orm_aliases) == 1
        assert next(iter(orm_aliases.values())).group == "old_group"

        # Parser should pick up group change.
        alias.group = "new_group"
        asset_op = AssetModelOperation.collect({dag.dag_id: dag})
        orm_aliases = asset_op.sync_asset_aliases(session=session)
        assert len(orm_aliases) == 1
        assert next(iter(orm_aliases.values())).group == "new_group"


@pytest.mark.db_test
@pytest.mark.want_activate_assets(False)
class TestAssetModelOperationSyncAssetActive:
    @staticmethod
    def clean_db():
        clear_db_dags()
        clear_db_assets()
        clear_db_triggers()

    @pytest.fixture(autouse=True)
    def per_test(self) -> Generator:
        self.clean_db()
        yield
        self.clean_db()

    def test_add_asset_activate(self, dag_maker, session):
        asset = Asset("myasset", "file://myasset/", group="old_group")
        with dag_maker(schedule=[asset]) as dag:
            EmptyOperator(task_id="mytask")

        asset_op = AssetModelOperation.collect({dag.dag_id: dag})
        orm_assets = asset_op.sync_assets(session=session)
        session.flush()
        assert len(orm_assets) == 1

        asset_op.activate_assets_if_possible(orm_assets.values(), session=session)
        session.flush()
        assert orm_assets["myasset", "file://myasset/"].active is not None

    def test_add_asset_activate_already_exists(self, dag_maker, session):
        asset = Asset("myasset", "file://myasset/", group="old_group")

        session.add(AssetModel.from_public(asset))
        session.flush()
        session.add(AssetActive.for_asset(asset))
        session.flush()

        with dag_maker(schedule=[asset]) as dag:
            EmptyOperator(task_id="mytask")

        asset_op = AssetModelOperation.collect({dag.dag_id: dag})
        orm_assets = asset_op.sync_assets(session=session)
        session.flush()
        assert len(orm_assets) == 1

        asset_op.activate_assets_if_possible(orm_assets.values(), session=session)
        session.flush()
        assert orm_assets["myasset", "file://myasset/"].active is not None, "should pick up existing active"

    @pytest.mark.parametrize(
        "existing_assets",
        [
            pytest.param([Asset("myasset", uri="file://different/asset")], id="name"),
            pytest.param([Asset("another", uri="file://myasset/")], id="uri"),
        ],
    )
    def test_add_asset_activate_conflict(self, dag_maker, session, existing_assets):
        session.add_all(AssetModel.from_public(a) for a in existing_assets)
        session.flush()
        session.add_all(AssetActive.for_asset(a) for a in existing_assets)
        session.flush()

        asset = Asset(name="myasset", uri="file://myasset/", group="old_group")
        with dag_maker(schedule=[asset]) as dag:
            EmptyOperator(task_id="mytask")

        asset_op = AssetModelOperation.collect({dag.dag_id: dag})
        orm_assets = asset_op.sync_assets(session=session)
        session.flush()
        assert len(orm_assets) == 1

        asset_op.activate_assets_if_possible(orm_assets.values(), session=session)
        session.flush()
        assert orm_assets["myasset", "file://myasset/"].active is None, "should not activate due to conflict"


@pytest.mark.db_test
class TestUpdateDagParsingResults:
    """Tests centred around the ``update_dag_parsing_results_in_db`` function."""

    @pytest.fixture
    def clean_db(self, session):
        yield
        clear_db_serialized_dags()
        clear_db_dags()
        clear_db_import_errors()

    @pytest.fixture(name="dag_import_error_listener")
    def _dag_import_error_listener(self):
        from unit.listeners import dag_import_error_listener

        get_listener_manager().add_listener(dag_import_error_listener)
        yield dag_import_error_listener
        get_listener_manager().clear()
        dag_import_error_listener.clear()

    def dag_to_lazy_serdag(self, dag: DAG) -> LazyDeserializedDAG:
        ser_dict = SerializedDAG.to_dict(dag)
        return LazyDeserializedDAG(data=ser_dict)

    @pytest.mark.skipif(
        condition="FabAuthManager" not in conf.get("core", "auth_manager"),
        reason="This is only for FabAuthManager",
    )
    @pytest.mark.usefixtures("clean_db")  # sync_perms in fab has bad session commit hygiene
    def test_sync_perms_syncs_dag_specific_perms_on_update(
        self, monkeypatch, spy_agency: SpyAgency, session, time_machine, testing_dag_bundle
    ):
        """
        Test that dagbag.sync_to_db will sync DAG specific permissions when a DAG is
        new or updated
        """
        from airflow import settings

        serialized_dags_count = session.query(func.count(SerializedDagModel.dag_id)).scalar()
        assert serialized_dags_count == 0

        monkeypatch.setattr(settings, "MIN_SERIALIZED_DAG_UPDATE_INTERVAL", 5)
        time_machine.move_to(tz.datetime(2020, 1, 5, 0, 0, 0), tick=False)

        dag = DAG(dag_id="test")

        sync_perms_spy = spy_agency.spy_on(
            airflow.dag_processing.collection._sync_dag_perms,
            call_original=False,
        )

        def _sync_to_db():
            sync_perms_spy.reset_calls()
            time_machine.shift(20)

            update_dag_parsing_results_in_db("testing", None, [dag], dict(), set(), session)

        _sync_to_db()
        spy_agency.assert_spy_called_with(sync_perms_spy, dag, session=session)

        # DAG isn't updated
        _sync_to_db()
        spy_agency.assert_spy_not_called(sync_perms_spy)

        # DAG is updated
        dag.tags = {"new_tag"}
        _sync_to_db()
        spy_agency.assert_spy_called_with(sync_perms_spy, dag, session=session)

        serialized_dags_count = session.query(func.count(SerializedDagModel.dag_id)).scalar()

    @patch.object(SerializedDagModel, "write_dag")
    @patch("airflow.models.dag.DAG.bulk_write_to_db")
    def test_sync_to_db_is_retried(
        self, mock_bulk_write_to_db, mock_s10n_write_dag, testing_dag_bundle, session
    ):
        """Test that important DB operations in db sync are retried on OperationalError"""
        serialized_dags_count = session.query(func.count(SerializedDagModel.dag_id)).scalar()
        assert serialized_dags_count == 0
        mock_dag = mock.MagicMock()
        dags = [mock_dag]

        op_error = OperationalError(statement=mock.ANY, params=mock.ANY, orig=mock.ANY)

        # Mock error for the first 2 tries and a successful third try
        side_effect = [op_error, op_error, mock.ANY]

        mock_bulk_write_to_db.side_effect = side_effect

        mock_session = mock.MagicMock()
        update_dag_parsing_results_in_db(
            "testing", None, dags=dags, import_errors={}, warnings=set(), session=mock_session
        )

        # Test that 3 attempts were made to run 'DAG.bulk_write_to_db' successfully
        mock_bulk_write_to_db.assert_has_calls(
            [
                mock.call("testing", None, mock.ANY, session=mock.ANY),
                mock.call("testing", None, mock.ANY, session=mock.ANY),
                mock.call("testing", None, mock.ANY, session=mock.ANY),
            ]
        )
        # Assert that rollback is called twice (i.e. whenever OperationalError occurs)
        mock_session.rollback.assert_has_calls([mock.call(), mock.call()])
        # Check that 'SerializedDagModel.write_dag' is also called
        # Only called once since the other two times the 'DAG.bulk_write_to_db' error'd
        # and the session was roll-backed before even reaching 'SerializedDagModel.write_dag'
        mock_s10n_write_dag.assert_has_calls(
            [
                mock.call(
                    mock_dag,
                    bundle_name="testing",
                    bundle_version=None,
                    min_update_interval=mock.ANY,
                    session=mock_session,
                ),
            ]
        )

        serialized_dags_count = session.query(func.count(SerializedDagModel.dag_id)).scalar()
        assert serialized_dags_count == 0

    def test_serialized_dags_are_written_to_db_on_sync(self, testing_dag_bundle, session):
        """
        Test that when dagbag.sync_to_db is called the DAGs are Serialized and written to DB
        even when dagbag.read_dags_from_db is False
        """
        serialized_dags_count = session.query(func.count(SerializedDagModel.dag_id)).scalar()
        assert serialized_dags_count == 0

        dag = DAG(dag_id="test")

        update_dag_parsing_results_in_db("testing", None, [dag], dict(), set(), session)

        new_serialized_dags_count = session.query(func.count(SerializedDagModel.dag_id)).scalar()
        assert new_serialized_dags_count == 1

    @patch.object(ParseImportError, "full_file_path")
    @patch.object(SerializedDagModel, "write_dag")
    @pytest.mark.usefixtures("clean_db")
    def test_serialized_dag_errors_are_import_errors(
        self, mock_serialize, mock_full_path, caplog, session, dag_import_error_listener, testing_dag_bundle
    ):
        """
        Test that errors serializing a DAG are recorded as import_errors in the DB
        """
        mock_serialize.side_effect = SerializationError
        caplog.set_level(logging.ERROR)

        dag = DAG(dag_id="test")
        dag.fileloc = "abc.py"
        dag.relative_fileloc = "abc.py"
        mock_full_path.return_value = "abc.py"

        import_errors = {}
        update_dag_parsing_results_in_db("testing", None, [dag], import_errors, set(), session)
        assert "SerializationError" in caplog.text

        # Should have been edited in place
        err = import_errors.get(("testing", dag.relative_fileloc))
        assert "SerializationError" in err
        dag_model: DagModel = session.get(DagModel, (dag.dag_id,))
        assert dag_model.has_import_errors is True

        import_errors = session.query(ParseImportError).all()

        assert len(import_errors) == 1
        import_error = import_errors[0]
        assert import_error.filename == dag.relative_fileloc
        assert "SerializationError" in import_error.stacktrace

        # Ensure the listener was notified
        assert len(dag_import_error_listener.new) == 1
        assert len(dag_import_error_listener.existing) == 0
        assert dag_import_error_listener.new["abc.py"] == import_error.stacktrace

    @patch.object(ParseImportError, "full_file_path")
    @pytest.mark.usefixtures("clean_db")
    def test_new_import_error_replaces_old(
        self, mock_full_file_path, session, dag_import_error_listener, testing_dag_bundle
    ):
        """
        Test that existing import error is updated and new record not created
        for a dag with the same filename
        """
        bundle_name = "testing"
        filename = "abc.py"
        mock_full_file_path.return_value = filename
        prev_error = ParseImportError(
            filename=filename,
            bundle_name=bundle_name,
            timestamp=tz.utcnow(),
            stacktrace="Some error",
        )
        session.add(prev_error)
        session.flush()
        prev_error_id = prev_error.id

        update_dag_parsing_results_in_db(
            bundle_name=bundle_name,
            bundle_version=None,
            dags=[],
            import_errors={("testing", "abc.py"): "New error"},
            warnings=set(),
            session=session,
        )

        import_error = (
            session.query(ParseImportError)
            .filter(ParseImportError.filename == filename, ParseImportError.bundle_name == bundle_name)
            .one()
        )

        # assert that the ID of the import error did not change
        assert import_error.id == prev_error_id
        assert import_error.stacktrace == "New error"

        # Ensure the listener was notified
        assert len(dag_import_error_listener.new) == 0
        assert len(dag_import_error_listener.existing) == 1
        assert dag_import_error_listener.existing["abc.py"] == prev_error.stacktrace

    @pytest.mark.usefixtures("clean_db")
    def test_remove_error_clears_import_error(self, testing_dag_bundle, session):
        # Pre-condition: there is an import error for the dag file
        bundle_name = "testing"
        filename = "abc.py"
        prev_error = ParseImportError(
            filename=filename,
            bundle_name=bundle_name,
            timestamp=tz.utcnow(),
            stacktrace="Some error",
        )
        session.add(prev_error)

        # And one for another file we haven't been given results for -- this shouldn't be deleted
        session.add(
            ParseImportError(
                filename="def.py",
                bundle_name=bundle_name,
                timestamp=tz.utcnow(),
                stacktrace="Some error",
            )
        )
        session.flush()

        # Sanity check of pre-condition
        import_errors = set(session.execute(select(ParseImportError.filename, ParseImportError.bundle_name)))
        assert import_errors == {("abc.py", bundle_name), ("def.py", bundle_name)}

        dag = DAG(dag_id="test")
        dag.fileloc = filename
        dag.relative_fileloc = filename

        import_errors = {}
        update_dag_parsing_results_in_db(bundle_name, None, [dag], import_errors, set(), session)

        dag_model: DagModel = session.get(DagModel, (dag.dag_id,))
        assert dag_model.has_import_errors is False

        import_errors = set(session.execute(select(ParseImportError.filename, ParseImportError.bundle_name)))

        assert import_errors == {("def.py", bundle_name)}

    @pytest.mark.usefixtures("clean_db")
    def test_remove_error_updates_loaded_dag_model(self, testing_dag_bundle, session):
        bundle_name = "testing"
        filename = "abc.py"
        session.add(
            ParseImportError(
                filename=filename,
                bundle_name=bundle_name,
                timestamp=tz.utcnow(),
                stacktrace="Some error",
            )
        )
        session.add(
            ParseImportError(
                filename="def.py",
                bundle_name=bundle_name,
                timestamp=tz.utcnow(),
                stacktrace="Some error",
            )
        )
        session.flush()
        dag = DAG(dag_id="test")
        dag.fileloc = filename
        dag.relative_fileloc = filename
        import_errors = {(bundle_name, filename): "Some error"}
        update_dag_parsing_results_in_db(bundle_name, None, [dag], import_errors, set(), session)
        dag_model = session.get(DagModel, (dag.dag_id,))
        assert dag_model.has_import_errors is True
        import_errors = {}
        update_dag_parsing_results_in_db(bundle_name, None, [dag], import_errors, set(), session)
        assert dag_model.has_import_errors is False

    @pytest.mark.parametrize(
        ("attrs", "expected"),
        [
            pytest.param(
                {
                    "_tasks_": [
                        EmptyOperator(task_id="task", owner="owner1"),
                        EmptyOperator(task_id="task2", owner="owner2"),
                        EmptyOperator(task_id="task3"),
                        EmptyOperator(task_id="task4", owner="owner2"),
                    ]
                },
                {"owners": ["owner1", "owner2"]},
                id="tasks-multiple-owners",
            ),
            pytest.param(
                {"is_paused_upon_creation": True},
                {"is_paused": True},
                id="is_paused_upon_creation",
            ),
            pytest.param(
                {},
                {"owners": ["airflow"]},
                id="default-owner",
            ),
            pytest.param(
                {
                    "_tasks_": [
                        EmptyOperator(task_id="task", owner="owner1"),
                        EmptyOperator(task_id="task2", owner="owner2"),
                        EmptyOperator(task_id="task3"),
                        EmptyOperator(task_id="task4", owner="owner2"),
                    ],
                    "schedule": "0 0 * * *",
                    "catchup": False,
                },
                {
                    "owners": ["owner1", "owner2"],
                    "next_dagrun": tz.datetime(2020, 1, 5, 0, 0, 0),
                    "next_dagrun_data_interval_start": tz.datetime(2020, 1, 5, 0, 0, 0),
                    "next_dagrun_data_interval_end": tz.datetime(2020, 1, 6, 0, 0, 0),
                    "next_dagrun_create_after": tz.datetime(2020, 1, 6, 0, 0, 0),
                },
                id="with-scheduled-dagruns",
            ),
        ],
    )
    @pytest.mark.usefixtures("clean_db")
    def test_dagmodel_properties(self, attrs, expected, session, time_machine, testing_dag_bundle, dag_maker):
        """Test that properties on the dag model are correctly set when dealing with a LazySerializedDag"""
        dt = tz.datetime(2020, 1, 5, 0, 0, 0)
        time_machine.move_to(dt, tick=False)

        tasks = attrs.pop("_tasks_", None)
        with dag_maker("dag", **attrs) as dag:
            ...
        if tasks:
            dag.add_tasks(tasks)

        if attrs.pop("schedule", None):
            dr_kwargs = {
                "dag_id": "dag",
                "run_type": "scheduled",
                "data_interval": (dt, dt + timedelta(minutes=5)),
            }
            dr1 = DagRun(logical_date=dt, run_id="test_run_id_1", **dr_kwargs, start_date=dt)
            session.add(dr1)
            session.commit()
        update_dag_parsing_results_in_db("testing", None, [self.dag_to_lazy_serdag(dag)], {}, set(), session)

        orm_dag = session.get(DagModel, ("dag",))

        for attrname, expected_value in expected.items():
            if attrname == "owners":
                assert sorted(orm_dag.owners.split(", ")) == expected_value
            else:
                assert getattr(orm_dag, attrname) == expected_value

        assert orm_dag.last_parsed_time == dt

    def test_existing_dag_is_paused_upon_creation(self, testing_dag_bundle, session, dag_maker):
        with dag_maker("dag_paused", schedule=None) as dag:
            ...
        update_dag_parsing_results_in_db("testing", None, [self.dag_to_lazy_serdag(dag)], {}, set(), session)
        orm_dag = session.get(DagModel, ("dag_paused",))
        assert orm_dag.is_paused is False

        with dag_maker("dag_paused", schedule=None, is_paused_upon_creation=True) as dag:
            ...
        update_dag_parsing_results_in_db("testing", None, [self.dag_to_lazy_serdag(dag)], {}, set(), session)
        # Since the dag existed before, it should not follow the pause flag upon creation
        orm_dag = session.get(DagModel, ("dag_paused",))
        assert orm_dag.is_paused is False

    def test_bundle_name_and_version_are_stored(self, testing_dag_bundle, session, dag_maker):
        with dag_maker("mydag", schedule=None) as dag:
            ...
        update_dag_parsing_results_in_db("testing", "1.0", [self.dag_to_lazy_serdag(dag)], {}, set(), session)
        orm_dag = session.get(DagModel, "mydag")
        assert orm_dag.bundle_name == "testing"
        assert orm_dag.bundle_version == "1.0"
