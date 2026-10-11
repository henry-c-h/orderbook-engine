from pathlib import Path

from builders import buy
from strategies import records_for

from orderbook_engine.adapters.file_log import FileLog
from orderbook_engine.app.event_log import EventLog
from orderbook_engine.app.memory_log import InMemoryLog
from orderbook_engine.domain.commands import Cancel, Submit


def append_and_read_back(log: EventLog) -> int:
    """Uses a log only through the EventLog interface; pyright checks the type."""
    for record in records_for([Submit(buy("b1", 100)), Cancel("b1")]):
        log.append(record)
    return len(list(log.read_records()))


def test_file_log_and_in_memory_log_are_both_event_logs(tmp_path: Path) -> None:
    # given one log of each implementation
    in_memory = InMemoryLog()

    # when each is used through the EventLog interface
    with FileLog(tmp_path / "events.log") as file_log:
        file_count = append_and_read_back(file_log)
    memory_count = append_and_read_back(in_memory)

    # then both accept the records and read them back
    assert file_count == 2
    assert memory_count == 2
