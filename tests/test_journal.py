import tempfile
import threading
import unittest
from pathlib import Path
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
