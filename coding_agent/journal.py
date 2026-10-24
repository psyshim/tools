"""Durable idempotency journal for external effects. Does not execute effects."""
import hashlib
import json
import sqlite3
from pathlib import Path

class OperationConflict(Exception):
    pass

class OperationJournal:
    def __init__(self, filename):
        location = Path(filename)
        location.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(location), timeout=10, isolation_level=None)
        self.conn.execute('PRAGMA busy_timeout=10000')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS operations
            (id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL,
             result TEXT, CHECK(state IN ('PREPARED','UNKNOWN','SUCCEEDED','FAILED')))''')

    def prepare(self, operation_id, request):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            raise ValueError('invalid operation ID')
        raw = json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            self.conn.execute('INSERT OR IGNORE INTO operations(id,digest,state) VALUES(?,?,?)',
                              (operation_id,digest,'PREPARED'))
            row = self.conn.execute('SELECT digest,state,result FROM operations WHERE id=?', (operation_id,)).fetchone()
            if row[0] != digest:
                raise OperationConflict('operation ID reused for different request')
            self.conn.execute('COMMIT')
            return {'state':row[1], 'result':json.loads(row[2]) if row[2] else None}
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    def claim(self, operation_id):
        """Atomically claim a prepared effect before running it, once globally.

        UNKNOWN means claimed or possibly applied; no automatic retry.
        """
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            cursor = self.conn.execute(
                "UPDATE operations SET state='UNKNOWN' WHERE id=? AND state='PREPARED'",
                (operation_id,))
            won = cursor.rowcount == 1
            self.conn.execute('COMMIT')
            return won
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    def mark_uncertain(self, operation_id):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            cursor = self.conn.execute("UPDATE operations SET state='UNKNOWN' WHERE id=? AND state='PREPARED'", (operation_id,))
            if cursor.rowcount != 1:
                raise OperationConflict('not a prepared operation')
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    def reconcile(self, operation_id, result, succeeded):
        payload = json.dumps(result, sort_keys=True, allow_nan=False)
        state = 'SUCCEEDED' if succeeded else 'FAILED'
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute('SELECT state,result FROM operations WHERE id=?', (operation_id,)).fetchone()
            if row is None or (row[0] in ('SUCCEEDED','FAILED') and (row[0] != state or row[1] != payload)):
                raise OperationConflict('operation missing or conflicting final result')
            if row[0] in ('PREPARED','UNKNOWN'):
                self.conn.execute('UPDATE operations SET state=?,result=? WHERE id=?', (state,payload,operation_id))
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    def close(self):
        self.conn.close()
