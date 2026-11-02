"""Collections that cache shallow, immutable membership snapshots."""

from collections import deque


def _invalidating(method):
    def mutate(self, *args, **kwargs):
        # Invalidate even if an iterator raises after partially modifying a collection.
        try:
            return method(self, *args, **kwargs)
        finally:
            self._snapshot = None
    return mutate


class SnapshotList(list):
    def __init__(self, iterable=()):
        super().__init__(iterable)
        self._snapshot = None

    def snapshot(self):
        if self._snapshot is None:
            self._snapshot = tuple(self)
        return self._snapshot


class SnapshotDeque(deque):
    def __init__(self, iterable=(), maxlen=None):
        super().__init__(iterable, maxlen)
        self._snapshot = None

    def snapshot(self):
        if self._snapshot is None:
            self._snapshot = tuple(self)
        return self._snapshot


for _cls, _base, _methods in (
    (SnapshotList, list, ('append', 'extend', 'insert', 'pop', 'remove', 'clear',
                          '__setitem__', '__delitem__', '__iadd__', '__imul__',
                          'reverse', 'sort')),
    (SnapshotDeque, deque, ('append', 'appendleft', 'extend', 'extendleft', 'pop',
                            'popleft', 'remove', 'clear', 'insert', '__setitem__',
                            '__delitem__', '__iadd__', '__imul__', 'reverse', 'rotate')),
):
    for _name in _methods:
        setattr(_cls, _name, _invalidating(getattr(_base, _name)))
