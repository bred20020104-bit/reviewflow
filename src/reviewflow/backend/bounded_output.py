"""Drain subprocess output without unbounded memory or disk capture."""


class BoundedOutput:
    """A pipe writer for subprocess.run; a reader keeps only a fixed byte prefix.

    Self-contained so the trusted container controller can use the same code.
    Target output is never interpreted here and must still be validated by callers.
    """

    def __init__(self, limit=65536):
        self.limit = limit
        self.data = bytearray()
        self.overflow = False
        self.incomplete = False

    def __enter__(self):
        import os
        import threading

        self._read, write = os.pipe()
        self.writer = os.fdopen(write, "wb", buffering=0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()
        return self

    def _drain(self):
        import os
        import select

        after_stop = 0
        try:
            while True:
                ready, _, _ = select.select([self._read], [], [], 0.05)
                if not ready:
                    if self._stop.is_set():
                        break
                    continue
                block = os.read(self._read, 8192)
                if not block:
                    break
                space = self.limit - len(self.data)
                self.data.extend(block[:space])
                self.overflow |= len(block) > space
                if self._stop.is_set():
                    after_stop += 1
                    if after_stop > self.limit // 8192 + 1:
                        self.incomplete = True
                        break
        except OSError:
            self.incomplete = True
        finally:
            os.close(self._read)

    def __exit__(self, *_):
        self.writer.close()
        self._stop.set()
        self._thread.join()
