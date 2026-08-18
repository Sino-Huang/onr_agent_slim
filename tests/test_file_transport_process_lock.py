from __future__ import annotations

from multiprocessing import Pipe, Process
from pathlib import Path

import pytest

from onr.adapters.file_transport import FileTransport
from onr.ports.transport import Subscription


def _hold_consumer(root: str, ready: object) -> None:
    subscription = Subscription("worker", "mission", "topic")
    transport = FileTransport(Path(root), (subscription,))
    transport.open_consumer(subscription)
    ready.send(True)  # type: ignore[union-attr]
    # The process is deliberately terminated by the parent without close().
    ready.recv()  # type: ignore[union-attr]


@pytest.mark.skipif(not hasattr(__import__("fcntl"), "flock"), reason="requires POSIX flock")
def test_file_consumer_lock_is_process_exclusive_and_released_on_exit(tmp_path: Path) -> None:
    parent, child = Pipe()
    process = Process(target=_hold_consumer, args=(str(tmp_path), child))
    process.start()
    assert parent.recv() is True
    subscription = Subscription("worker", "mission", "topic")
    with pytest.raises(RuntimeError):
        FileTransport(tmp_path, (subscription,)).open_consumer(subscription)
    process.terminate()
    process.join(timeout=5)
    assert process.exitcode is not None
    restarted = FileTransport(tmp_path, (subscription,))
    consumer = restarted.open_consumer(subscription)
    consumer.close()
