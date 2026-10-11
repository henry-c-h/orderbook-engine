from typing import Literal

import pytest
from builders import three_records
from strategies import records_for

from orderbook_engine.app.event_log import LogFailedError
from orderbook_engine.app.memory_log import InMemoryLog
from orderbook_engine.domain.commands import Cancel


class TestAppendAndRead:
    def test_a_new_log_has_no_records(self) -> None:
        # given a new log
        log = InMemoryLog()

        # when its records are read
        records = list(log.read_records())

        # then there are none
        assert records == []

    def test_records_read_back_equal_to_the_records_appended(self) -> None:
        # given a log and three records
        records = three_records()
        log = InMemoryLog()

        # when they are appended
        for record in records:
            log.append(record)

        # then they read back in order
        assert list(log.read_records()) == records


class TestSeqRules:
    def test_the_first_record_must_have_seq_1(self) -> None:
        # given an empty log and a record with seq 2
        _, second, _ = three_records()
        log = InMemoryLog()

        # when it is appended
        # then it raises, and nothing is stored
        with pytest.raises(ValueError, match="seq"):
            log.append(second)
        assert list(log.read_records()) == []

    def test_a_record_with_the_wrong_seq_is_refused(self) -> None:
        # given a log holding one record
        first, second, third = three_records()
        log = InMemoryLog()
        log.append(first)

        # when a record that is not the next one is appended
        # then it raises, whether it repeats a seq or skips one
        with pytest.raises(ValueError, match="seq"):
            log.append(first)
        with pytest.raises(ValueError, match="seq"):
            log.append(third)

        # and nothing was stored: the next record is still seq 2
        assert list(log.read_records()) == [first]
        log.append(second)
        assert list(log.read_records()) == [first, second]


class TestInjectedFailure:
    @pytest.mark.parametrize("mode", ["before", "after"])
    def test_records_before_the_failing_seq_are_stored_normally(
        self, mode: Literal["before", "after"]
    ) -> None:
        # given a log set to fail at seq 3
        first, second, _ = three_records()
        log = InMemoryLog(fail_at_seq=3, fail_mode=mode)

        # when records 1 and 2 are appended
        log.append(first)
        log.append(second)

        # then both are stored and nothing raised
        assert list(log.read_records()) == [first, second]

    def test_failing_before_raises_os_error_and_does_not_store_the_record(
        self,
    ) -> None:
        # given a log set to fail before storing seq 2
        first, second, _ = three_records()
        log = InMemoryLog(fail_at_seq=2, fail_mode="before")
        log.append(first)

        # when seq 2 is appended
        # then OSError is raised
        with pytest.raises(OSError, match="seq 2"):
            log.append(second)

        # and the record is not stored
        assert list(log.read_records()) == [first]

    def test_failing_after_raises_os_error_but_stores_the_record(self) -> None:
        # given a log set to fail after storing seq 2
        first, second, _ = three_records()
        log = InMemoryLog(fail_at_seq=2, fail_mode="after")
        log.append(first)

        # when seq 2 is appended
        # then OSError is raised
        with pytest.raises(OSError, match="seq 2"):
            log.append(second)

        # and the record is stored
        assert list(log.read_records()) == [first, second]

    @pytest.mark.parametrize("mode", ["before", "after"])
    def test_after_a_failure_every_append_raises_log_failed_error(
        self, mode: Literal["before", "after"]
    ) -> None:
        # given a log that has failed at seq 2
        first, second, third = three_records()
        log = InMemoryLog(fail_at_seq=2, fail_mode=mode)
        log.append(first)
        with pytest.raises(OSError, match="seq 2"):
            log.append(second)
        stored = list(log.read_records())

        # when more appends are attempted, including the next valid seq
        # then each raises LogFailedError
        with pytest.raises(LogFailedError):
            log.append(second)
        with pytest.raises(LogFailedError):
            log.append(third)

        # and what was stored does not change
        assert list(log.read_records()) == stored

    def test_a_failed_log_can_still_be_read(self) -> None:
        # given a log that has failed after storing seq 1
        first, _, _ = three_records()
        log = InMemoryLog(fail_at_seq=1, fail_mode="after")
        with pytest.raises(OSError, match="seq 1"):
            log.append(first)

        # when it is read
        records = list(log.read_records())

        # then it returns the stored record
        assert records == [first]

    def test_the_default_mode_is_before(self) -> None:
        # given a log set to fail at seq 1 without a mode
        first, _, _ = three_records()
        log = InMemoryLog(fail_at_seq=1)

        # when seq 1 is appended
        with pytest.raises(OSError, match="seq 1"):
            log.append(first)

        # then the record is not stored
        assert list(log.read_records()) == []

    def test_without_fail_at_seq_the_log_never_fails(self) -> None:
        # given a log with no failure set, and many records
        records = records_for([Cancel(f"c{n}") for n in range(50)])
        log = InMemoryLog()

        # when they are all appended
        for record in records:
            log.append(record)

        # then all are stored
        assert list(log.read_records()) == records

    @pytest.mark.parametrize("fail_at_seq", [0, -1])
    def test_a_fail_at_seq_below_1_is_refused(self, fail_at_seq: int) -> None:
        # given a fail_at_seq that no record can have
        # when a log is built with it
        # then it raises
        with pytest.raises(ValueError, match="fail_at_seq"):
            InMemoryLog(fail_at_seq=fail_at_seq)

    def test_a_fail_at_seq_of_1_is_accepted(self) -> None:
        # given the lowest valid fail_at_seq
        # when a log is built with it
        log = InMemoryLog(fail_at_seq=1)

        # then it is built and holds no records
        assert list(log.read_records()) == []


class TestFromRecords:
    def test_it_holds_the_given_records_in_order(self) -> None:
        # given three records
        records = three_records()

        # when a log is built from them
        log = InMemoryLog.from_records(records)

        # then it reads them back in order
        assert list(log.read_records()) == records

    def test_it_accepts_no_records(self) -> None:
        # given no records
        # when a log is built from them
        log = InMemoryLog.from_records([])

        # then it is empty and the first append must be seq 1
        assert list(log.read_records()) == []
        first = three_records()[0]
        log.append(first)
        assert list(log.read_records()) == [first]

    def test_appending_continues_after_the_given_records(self) -> None:
        # given a log built from the first two records
        first, second, third = three_records()
        log = InMemoryLog.from_records([first, second])

        # when the third is appended
        log.append(third)

        # then it is stored after them
        assert list(log.read_records()) == [first, second, third]

    @pytest.mark.parametrize("skip", [[1], [0, 2], [0, 0]])
    def test_records_that_break_the_seq_rules_are_refused(
        self, skip: list[int]
    ) -> None:
        # given records that start at seq 2, skip a seq or repeat one
        all_records = three_records()
        records = [all_records[index] for index in skip]

        # when a log is built from them
        # then it raises
        with pytest.raises(ValueError, match="seq"):
            InMemoryLog.from_records(records)

    def test_the_result_is_not_failed_even_if_the_source_log_was(self) -> None:
        # given a log that failed at seq 2, after storing it
        first, second, third = three_records()
        failed = InMemoryLog(fail_at_seq=2, fail_mode="after")
        failed.append(first)
        with pytest.raises(OSError, match="seq 2"):
            failed.append(second)

        # when a fresh log is built from what it stored
        fresh = InMemoryLog.from_records(failed.read_records())

        # then it accepts the next record
        fresh.append(third)
        assert list(fresh.read_records()) == [first, second, third]

    @pytest.mark.parametrize(("mode", "stored"), [("before", 1), ("after", 2)])
    def test_failure_injection_applies_to_later_appends(
        self, mode: Literal["before", "after"], stored: int
    ) -> None:
        # given a log built from one record, set to fail at seq 2
        first, second, third = three_records()
        log = InMemoryLog.from_records([first], fail_at_seq=2, fail_mode=mode)

        # when seq 2 is appended
        # then it raises OSError, and later appends raise LogFailedError
        with pytest.raises(OSError, match="seq 2"):
            log.append(second)
        with pytest.raises(LogFailedError):
            log.append(third)

        # and the record is stored only in "after" mode
        assert list(log.read_records()) == [first, second][:stored]

    def test_failure_injection_does_not_affect_the_given_records(self) -> None:
        # given four records and a failure set at seq 2, inside the first three
        records = records_for([Cancel(f"c{n}") for n in range(4)])

        # when a log is built from the first three
        log = InMemoryLog.from_records(records[:3], fail_at_seq=2)

        # then they are stored, and the log is not failed: seq 4 is accepted
        assert list(log.read_records()) == records[:3]
        log.append(records[3])
        assert list(log.read_records()) == records

    def test_a_fail_at_seq_below_1_is_refused(self) -> None:
        # given a fail_at_seq that no record can have
        # when a log is built with it
        # then it raises
        with pytest.raises(ValueError, match="fail_at_seq"):
            InMemoryLog.from_records([], fail_at_seq=0)
