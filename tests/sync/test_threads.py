import threading

import pytest

import drivine
from drivine.sync import PersistenceManager, StatelessGraphObjectManager, transaction, transactional
from tests.models import Person


def test_each_thread_has_its_own_transaction(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    seen: dict[str, object] = {}
    started = threading.Barrier(2)

    def work(name: str) -> None:
        with transaction() as tx:
            started.wait()
            gom.save(Person(uuid=name, name=name))
            seen[name] = tx

    threads = [threading.Thread(target=work, args=(it,)) for it in ("p1", "p2")]
    for it in threads:
        it.start()
    for it in threads:
        it.join()
    assert seen["p1"] is not seen["p2"]
    assert sorted(it.uuid for it in gom.load_all(Person)) == ["p1", "p2"]


@pytest.mark.rolls_back
def test_a_sync_transactional_function_rolls_back_on_failure(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)

    @transactional
    def save_then_fail() -> None:
        gom.save(Person(uuid="p1", name="Ada"))
        raise ValueError("roll back")

    with pytest.raises(ValueError):
        save_then_fail()
    assert gom.count(Person) == 0


def test_each_api_takes_its_own_kind_of_function():
    async def coroutine() -> None: ...

    def plain() -> None: ...

    with pytest.raises(TypeError, match="takes a plain function.*from drivine"):
        transactional(coroutine)  # pyright: ignore[reportArgumentType, reportCallIssue]
    with pytest.raises(TypeError, match="takes an async function.*from drivine.sync"):
        drivine.transactional(plain)  # pyright: ignore[reportArgumentType, reportCallIssue]
