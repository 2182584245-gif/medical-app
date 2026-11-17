"""Transaction-pinned SQLite adapter for isolated tests/fixtures, not cloud deployment."""
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

class AtomicSqliteDatabase:
    def __init__(self,database):
        self.wrapped=database
        self._current=ContextVar("expansion_sqlite_"+uuid4().hex,default=None)
    def __getattr__(self,name): return getattr(self.wrapped,name)
    @contextmanager
    def connect(self):
        current=self._current.get()
        if current is not None: yield current
        else:
            with self.wrapped.connect() as c: yield c
    @contextmanager
    def transaction(self,**kwargs):
        current=self._current.get()
        if current is not None:
            name="sp_"+uuid4().hex
            current.execute("SAVEPOINT "+name)
            try: yield current
            except Exception:
                current.execute("ROLLBACK TO SAVEPOINT "+name); current.execute("RELEASE SAVEPOINT "+name)
                raise
            else: current.execute("RELEASE SAVEPOINT "+name)
        else:
            with self.wrapped.transaction(**kwargs) as c:
                token=self._current.set(c)
                try: yield c
                finally: self._current.reset(token)
