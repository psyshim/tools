import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from coding_agent.journal import OperationJournal, OperationConflict

class JournalTests(unittest.TestCase):
    def test_reopen_preserves_idempotency_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            filename = Path(tmp)/'journal.db'
            first=OperationJournal(filename)
            self.assertEqual(first.prepare('one',{'path':'x','hash':'a'})['state'],'PREPARED')
            first.mark_uncertain('one')
            first.close()
            second=OperationJournal(filename)
            self.assertEqual(second.prepare('one',{'hash':'a','path':'x'})['state'],'UNKNOWN')
            second.reconcile('one',{'sha':'deadbeef'},True)
            self.assertEqual(second.prepare('one',{'path':'x','hash':'a'})['result'],{'sha':'deadbeef'})
            with self.assertRaises(OperationConflict):
                second.prepare('one',{'path':'other'})
            with self.assertRaises(OperationConflict):
                second.reconcile('one',{'sha':'different'},True)
            second.close()

    def test_two_connections_cannot_create_conflicting_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'j.db'
            results=[]
            def write(payload):
                j=OperationJournal(path)
                try:
                    results.append(j.prepare('same',payload)['state'])
                except OperationConflict:
                    results.append('CONFLICT')
                finally:
                    j.close()
            t1=threading.Thread(target=write,args=({'a':1},));t2=threading.Thread(target=write,args=({'a':2},))
            t1.start();t2.start();t1.join();t2.join()
            self.assertCountEqual(results,['PREPARED','CONFLICT'])

    def test_no_fake_result_after_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            j=OperationJournal(Path(tmp)/'x.db')
            j.prepare('id',{'x':1});j.mark_uncertain('id')
            self.assertEqual(j.prepare('id',{'x':1}),{'state':'UNKNOWN','result':None})
            with self.assertRaises(OperationConflict): j.mark_uncertain('id')
            j.close()

    def test_wal_busy_retries_then_succeeds(self):
        """SQLITE_BUSY on first WAL PRAGMA triggers bounded retry; journal opens normally."""
        with tempfile.TemporaryDirectory() as tmp:
            wal_calls = []
            real_connect = sqlite3.connect

            def intercept_connect(*args, **kwargs):
                conn = real_connect(*args, **kwargs)
                real_execute = conn.execute
                def execute_wrapper(sql, *params):
                    if 'journal_mode=WAL' in sql:
                        wal_calls.append(len(wal_calls) + 1)
                        if len(wal_calls) == 1:
                            exc = sqlite3.OperationalError('database is locked')
                            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
                            raise exc
                    return real_execute(sql, *params)
                proxy = unittest.mock.Mock(wraps=conn)
                proxy.execute.side_effect = execute_wrapper
                return proxy

            with patch('sqlite3.connect', intercept_connect):
                j = OperationJournal(Path(tmp) / 'wal_busy.db')
                # Journal opened successfully after retry
                self.assertEqual(j.prepare('op1', {'k': 'v'})['state'], 'PREPARED')
                j.close()
            # WAL pragma was attempted at least twice (first BUSY, then success)
            self.assertGreaterEqual(len(wal_calls), 2)

    def test_non_busy_error_closes_conn_without_retry(self):
        """Non-BUSY OperationalError during WAL init propagates and closes the connection."""
        with tempfile.TemporaryDirectory() as tmp:
            wal_calls = []
            close_calls = []
            real_connect = sqlite3.connect

            def intercept_connect(*args, **kwargs):
                conn = real_connect(*args, **kwargs)
                real_execute = conn.execute
                real_close = conn.close
                def execute_wrapper(sql, *params):
                    if 'journal_mode=WAL' in sql:
                        wal_calls.append(1)
                        exc = sqlite3.OperationalError('disk I/O error')
                        exc.sqlite_errorcode = 10  # SQLITE_IOERR, not BUSY/LOCKED
                        raise exc
                    return real_execute(sql, *params)
                def close_wrapper():
                    close_calls.append(1)
                    return real_close()
                proxy = unittest.mock.Mock(wraps=conn)
                proxy.execute.side_effect = execute_wrapper
                proxy.close.side_effect = close_wrapper
                return proxy

            with patch('sqlite3.connect', intercept_connect):
                with self.assertRaises(sqlite3.OperationalError):
                    OperationJournal(Path(tmp) / 'io_err.db')
            # WAL attempted exactly once — no retry for non-BUSY errors
            self.assertEqual(len(wal_calls), 1)
            # Connection was closed in the except BaseException handler
            self.assertGreaterEqual(len(close_calls), 1)
