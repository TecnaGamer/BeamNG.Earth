"""Queue world-server log records per process. The listener formats, writes and flushes them."""
import atexit
from collections import Counter
import logging
from logging.handlers import QueueHandler, QueueListener
import os
import queue
import signal
import threading


FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
CAPACITY = 2048
_active = None
_lifecycle = threading.Lock()


class _Records(queue.Queue):
    """FIFO. At capacity, a more important record can take a lower-level record's slot."""
    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("log queue capacity must be positive")
        super().__init__(capacity)
        self.dropped = Counter()
        self.counts = Counter()
        self.closing = False
        self.waiters = 0

    @staticmethod
    def band(record):
        if record.levelno < logging.INFO:
            return logging.DEBUG
        if record.levelno < logging.WARNING:
            return logging.INFO
        return logging.WARNING

    def _put(self, record):
        super()._put(record)
        if record is not None:
            self.counts[self.band(record)] += 1

    def _get(self):
        record = super()._get()
        if record is not None:
            self.counts[self.band(record)] -= 1
        return record

    def offer(self, record):
        band = self.band(record)
        with self.not_full:
            if self.closing:
                return False
            while self._qsize() == self.maxsize:
                victim = next((level for level in (logging.DEBUG, logging.INFO)
                               if level < band and self.counts[level]), None)
                if victim is not None:
                    for i, old in enumerate(self.queue):
                        if self.band(old) == victim:
                            del self.queue[i]
                            break
                    self.counts[victim] -= 1
                    self.dropped[victim] += 1
                    self.unfinished_tasks -= 1
                    break
                if band < logging.WARNING:
                    self.dropped[band] += 1
                    return True
                # Only protected records remain. Preserve them through a finite sink stall.
                self.waiters += 1
                try:
                    self.not_full.wait()
                finally:
                    self.waiters -= 1
                    if self.closing and not self.waiters:
                        self.all_tasks_done.notify_all()
            self._put(record)
            self.unfinished_tasks += 1
            self.not_empty.notify()
            return True

    def finish(self):
        with self.not_full:
            self.closing = True
            # Drain warnings already waiting for a slot before inserting the stop sentinel.
            self.all_tasks_done.wait_for(lambda: not self.waiters)

    def take_drops(self):
        with self.mutex:
            if not self.dropped:
                return None
            drops, self.dropped = self.dropped, Counter()
            return drops


class _Listener(QueueListener):
    def enqueue_sentinel(self):
        self.queue.put(self._sentinel)  # shutdown may find a full queue

    def handle(self, record):
        for handler in self.handlers:
            if record.levelno >= handler.level:
                try:
                    handler.handle(record)
                except Exception:
                    try:
                        handler.handleError(record)
                    except Exception:
                        # Even stderr may have been closed. Keep serving the other sinks.
                        pass

    def dequeue(self, block):
        record = super().dequeue(block)
        drops = self.queue.take_drops()
        if drops:
            self.handle(logging.LogRecord("earthd.logging", logging.WARNING, __file__, 0,
                        "log queue full: dropped %d DEBUG and %d INFO records",
                        (drops[logging.DEBUG], drops[logging.INFO]), None))
        return record


class _Handler(QueueHandler):
    def __init__(self, sinks, capacity):
        super().__init__(_Records(capacity))
        self.original = list(sinks)
        self.listener = _Listener(self.queue, *sinks)
        self.stop_request = None
        self.listener.start()

    def handle(self, record):
        # The queue synchronizes producers. A waiting warning must not hold up DEBUG/INFO.
        filtered = self.filter(record)
        if filtered:
            self.emit(filtered if isinstance(filtered, logging.LogRecord) else record)
        return filtered

    def emit(self, record):
        # Passing the original record between threads preserves exc_info for telemetry and
        # lets each sink format it on the listener, including exception tracebacks.
        if not self.queue.offer(record):  # shutdown raced a producer that had seen the old root handler
            self.listener.handle(record)


def configure(level, capacity=CAPACITY):
    global _active
    with _lifecycle:
        if _active is not None:
            return
        logging.basicConfig(level=level, format=FORMAT)
        root = logging.getLogger()
        handler = _Handler(root.handlers, capacity)
        root.handlers = [handler]
        _active = handler
        _drain_on_termination(handler)


def add_sink(sink):
    with _lifecycle:
        if _active is None:
            logging.getLogger().addHandler(sink)
        else:
            _active.listener.handlers += (sink,)


def shutdown():
    global _active
    watcher = None
    with _lifecycle:
        handler = _active
        if handler is None:
            return
        # Late exit callbacks can still log through the original sinks.
        logging.getLogger().handlers = handler.original
        handler.queue.finish()
        handler.listener.stop()
        for sink in handler.listener.handlers:
            try:
                sink.flush()
            except (OSError, ValueError):  # a stream already closed during exit
                pass
        if handler.stop_request is not None and threading.current_thread() is threading.main_thread():
            signum, wake, request, watcher = handler.stop_request
            if signal.getsignal(signum) is request:
                signal.signal(signum, signal.SIG_DFL)
            wake.put(False)
        _active = None
    if watcher is not None:
        watcher.join()


def _drain_on_termination(handler):
    """Drain for a Unix SIGTERM or a Windows console break.
    SimpleQueue.put is reentrant: the signal can interrupt a producer inside logging."""
    signum = signal.SIGBREAK if os.name == "nt" else signal.SIGTERM
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signum) != signal.SIG_DFL:
        return
    wake = queue.SimpleQueue()

    def terminate():
        if wake.get():
            shutdown()
            if os.name == "nt":
                os._exit(128 + signum)
            os.kill(os.getpid(), signum)  # preserve waitpid's signal status

    def request(signum, frame):
        signal.signal(signum, signal.SIG_DFL)
        wake.put(True)

    watcher = threading.Thread(target=terminate, name="log-terminate", daemon=True)
    watcher.start()
    handler.stop_request = signum, wake, request, watcher
    signal.signal(signum, request)


# Registered after logging.shutdown, so this runs first, including in spawned workers.
atexit.register(shutdown)
