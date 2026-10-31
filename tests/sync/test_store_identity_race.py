import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from drivine import uniqueness_constraint
from drivine.store.store_identity import StoreIdentity
from drivine.sync import PersistenceManager, StatelessGraphObjectManager
from tests.sync_integration.test_schema import RACE_ROUNDS, wipe_store_stamps


def test_store_identity_is_agreed_under_the_key_constraint_by_managers_racing_to_make_it(
    pm: PersistenceManager, caplog: pytest.LogCaptureFixture
):
    # #298, as drivine4j#473, as tests/integration/test_schema.py runs it with tasks: here each manager on a thread.
    stamps = StatelessGraphObjectManager(pm).backend.store_stamps().spec()

    def resolve(manager: StatelessGraphObjectManager) -> StoreIdentity:
        return manager.store_identity()

    with caplog.at_level(logging.WARNING, "drivine.store"), ThreadPoolExecutor(16) as threads:
        for _ in range(RACE_ROUNDS.get(pm.type, 5)):
            StatelessGraphObjectManager(pm).constraints.drop(uniqueness_constraint("DrivineStore", "key"))
            wipe_store_stamps(pm)
            managers = [StatelessGraphObjectManager(pm) for _ in range(16)]
            identities = list(threads.map(resolve, managers))
            assert len({it.id for it in identities}) == 1
            assert len(pm.query(stamps)) == 1
    assert [it.getMessage() for it in caplog.records if "best-effort" in it.getMessage()] == []
