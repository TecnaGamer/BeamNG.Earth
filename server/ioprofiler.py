"""Bounded, passive service and storage telemetry. Never sends probe traffic.

Services are identified by scheme/host/port only, without URLs, query strings,
headers or exception messages. Windows process I/O covers all handles, not just
physical disk. Instrumented cache reads provide separate file-operation timings.
"""
import collections
import contextvars
from contextlib import contextmanager, nullcontext
import functools
import http.client
import math
import dnp
import os
import shutil
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

_request = contextvars.ContextVar('earth_io_request', default=None)
MAX_SERVICES = 128
SAMPLE_KEEP = 64


def endpoint(url):
    try:
        u = urlsplit(url)
        host = u.hostname
        if not host or u.scheme not in ('http', 'https', 'tcp'):
            return None
        port = u.port
        host = '[' + host + ']' if ':' in host else host
        if port is not None and port != {'http': 80, 'https': 443}.get(u.scheme):
            host += ':' + str(port)
        return u.scheme + '://' + host.lower()
    except (ValueError, TypeError):
        return None


def process_io():
    """Cumulative OS counters, with explicit scope; unavailable values stay absent."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ('reads', 'writes', 'otherOps', 'readBytes', 'writeBytes', 'otherBytes')]
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters)]
        value = Counters()
        if not k.GetProcessIoCounters(k.GetCurrentProcess(), ctypes.byref(value)):
            return None
        return {'readBytes': value.readBytes, 'writeBytes': value.writeBytes,
                'readOps': value.reads, 'writeOps': value.writes, 'scope': 'all process I/O; not physical disk'}
    try:
        with open('/proc/self/io', encoding='ascii') as f:
            values = dict(line.strip().split(': ', 1) for line in f)
        return {'readBytes': int(values['read_bytes']), 'writeBytes': int(values['write_bytes']),
                'readOps': int(values['syscr']), 'writeOps': int(values['syscw']), 'scope': 'process storage I/O'}
    except (OSError, ValueError, KeyError):
        return None


class Monitor:
    def __init__(self):
        self.lock = threading.RLock()
        self.mode = 'off'
        self.session = None
        self.channel = self.control = None
        self.wake = threading.Event()
        self.thread = None
        self.reset()

    def reset(self):
        self.services = collections.OrderedDict()
        self.workers = {}
        self.disk = dict(readBytes=0, reads=0, misses=0, errors=0, readMs=0, timedReads=0,
                         maxReadMs=0, outputBytes=0, writesObserved=0)
        self.omitted = 0
        self.os_last = self.os_sample = None
        self.last_sample = self.last_flush = 0
        self.dirty = False

    def configure(self, mode, session=None, channel=None, control=None):
        with self.lock:
            if session != self.session or mode != self.mode:
                self.reset()
            self.mode, self.session, self.channel, self.control = mode, session, channel, control
            if mode != 'off' and channel is not None:
                if self.thread is None:
                    self.thread = threading.Thread(target=self._publish, daemon=True, name='profiler-io')
                    self.thread.start()
                self.wake.set()

    def _publish(self):
        while True:
            self.wake.wait()
            try:
                with self.lock:
                    if self.mode == 'off' or self.channel is None:
                        self.wake.clear()
                        continue
                    control = self.control
                if control is not None:
                    requested = control.value
                    if requested != (self.session, self.mode):
                        self.configure('off')
                        continue
                self.flush()
            except Exception:
                self.configure('off')
            time.sleep(1)

    def flush(self):
        with self.lock:
            if self.mode == 'off' or self.channel is None or time.perf_counter() - self.last_flush < 0.5:
                return
            channel, session = self.channel, self.session
            self.last_flush = time.perf_counter()
        try:
            channel.put_nowait((session, '__io', 'io', self.snapshot()))
        except Exception:
            pass

    def _service(self, name):
        row = self.services.get(name)
        if row is None:
            if len(self.services) >= MAX_SERVICES:
                # Never evict a currently active endpoint.
                victim = next((k for k, v in self.services.items() if not v['inflight']), None)
                if victim is None:
                    self.omitted += 1
                    return None
                del self.services[victim]
                self.omitted += 1
            row = dict(endpoint=name, requests=0, responses=0, failures=0, httpErrors=0, rateLimited=0,
                       inflight=0, receivedBytes=0, sentBytes=0, responseMs=0, responseSamples=0,
                       requestMs=0, completed=0, lastSeen=0, lastStatus=None, lastError=None,
                       samples=collections.deque(maxlen=SAMPLE_KEEP) if self.mode=='full' else None)
            self.services[name] = row
        self.services.move_to_end(name)
        return row

    def begin(self, url, sent_bytes=0):
        if self.mode == 'off':
            return None
        name = endpoint(url)
        if name is None:
            return None
        with self.lock:
            row = self._service(name)
            if row is None:
                return None
            row['requests'] += 1
            row['inflight'] += 1
            row['sentBytes'] += max(0, sent_bytes)
            row['lastSeen'] = time.perf_counter()
            return (self.session, name, row['lastSeen'])

    def finish(self, ticket, status=None, received=0, response_ms=None, error=None, sent=0):
        if ticket is None:
            return
        session, name, started = ticket
        now = time.perf_counter()
        with self.lock:
            if self.mode == 'off' or session != self.session or name not in self.services:
                return
            row = self.services[name]
            row['inflight'] = max(0, row['inflight'] - 1)
            row['lastSeen'] = now
            row['lastError'] = error
            row['lastStatus'] = status
            row['receivedBytes'] += max(0, received)
            row['sentBytes'] += max(0, sent)
            if error is not None:
                row['failures'] += 1
            else:
                row['responses'] += 1
                row['requestMs'] += (now - started) * 1000
                row['completed'] += 1
                if status is not None and status >= 400:
                    row['httpErrors'] += 1
                if status == 429:
                    row['rateLimited'] += 1
            if response_ms is not None and math.isfinite(response_ms) and response_ms >= 0:
                row['responseMs'] += response_ms
                row['responseSamples'] += 1
                if self.mode == 'full': row['samples'].append((now, response_ms))
            self.dirty = True

    def record_read(self, size, elapsed=None, failed=False, miss=False):
        if self.mode == 'off': return
        with self.lock:
            self.disk['reads'] += 1
            self.disk['readBytes'] += max(0, size)
            self.disk['misses'] += int(miss)
            self.disk['errors'] += int(failed)
            if elapsed is not None:
                self.disk['timedReads'] += 1
                self.disk['readMs'] += elapsed
                self.disk['maxReadMs'] = max(self.disk['maxReadMs'], elapsed)

    def progress(self, ticket, received=0, response_ms=None):
        if ticket is None: return False
        with self.lock:
            if self.mode == 'off' or ticket[0] != self.session or ticket[1] not in self.services:
                return False
            row = self.services[ticket[1]]
            row['receivedBytes'] += max(0, received)
            row['lastSeen'] = time.perf_counter()
            if response_ms is not None: row['lastResponseMs'] = response_ms
            return True

    def record_write(self, size):
        if self.mode == 'off': return
        with self.lock:
            self.disk['writesObserved'] += 1
            self.disk['outputBytes'] += max(0, size or 0)

    def snapshot(self):
        with self.lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        now = time.perf_counter()
        # Full only, at most once a second. Never performed on the game/event-loop thread.
        if self.mode == 'full' and now - self.last_sample >= 1:
            raw = process_io()
            if raw and self.os_last:
                at, previous = self.os_last
                dt = max(0.001, now - at)
                self.os_sample = {**raw, 'readBps': max(0, raw['readBytes']-previous['readBytes'])/dt,
                                  'writeBps': max(0, raw['writeBytes']-previous['writeBytes'])/dt}
            elif raw:
                self.os_sample = {**raw, 'readBps': None, 'writeBps': None}
            self.os_last, self.last_sample = (now, raw) if raw else None, now
        with self.lock:
            rows = []
            for row in self.services.values():
                copy = {k: v for k, v in row.items() if k != 'samples'}
                if self.mode == 'full': copy['samples'] = list(row['samples'])
                rows.append(copy)
            return dict(pid=os.getpid(), mode=self.mode, observed=now, services=rows, disk=dict(self.disk),
                        processIO=self.os_sample if self.mode == 'full' else None, omitted=self.omitted)


monitor = Monitor()


class Request:
    def __init__(self, url, sent_bytes=0):
        self.ticket = monitor.begin(url, sent_bytes)
        self.status = None
        self.received = 0
        self.response_ms = None
        self.sent = 0
        self.accounted = 0
        self.token = None

    def __enter__(self):
        if self.ticket is not None: self.token = _request.set(self)
        return self

    def headers(self):
        if self.ticket is not None and self.response_ms is None:
            self.response_ms = (time.perf_counter() - self.ticket[2]) * 1000
            monitor.progress(self.ticket, response_ms=self.response_ms)

    def add_received(self, size):
        self.received += size
        if monitor.progress(self.ticket, received=size): self.accounted += size

    def complete(self, status, received):
        self.status, self.received = status, received

    def __exit__(self, kind, value, tb):
        transport_error = isinstance(value, (OSError,http.client.HTTPException)) and not isinstance(value,urllib.error.HTTPError)
        monitor.finish(self.ticket, self.status, max(0,self.received-self.accounted), self.response_ms,
                       kind.__name__ if kind is not None and (self.status is None or transport_error) else None, self.sent)
        if self.token is not None: _request.reset(self.token)


def headers_received():
    request = _request.get()
    if request is not None: request.headers()


def peer_sent(size):
    request = _request.get()
    if request is not None: request.sent += size


def peer_received(size):
    request = _request.get()
    if request is not None: request.add_received(size)


received_bytes = peer_received


def peer_headers():
    request = _request.get()
    if request is not None and request.ticket is not None:
        request.response_ms = (time.perf_counter()-request.ticket[2])*1000
        monitor.progress(request.ticket, response_ms=request.response_ms)


def peer_operation(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        if monitor.mode == 'off': return fn(self, *args, **kwargs)
        host = '['+self.host+']' if ':' in self.host else self.host
        url = 'tcp://'+host+':'+str(self.port)
        previous = _request.get()
        if previous is not None and previous.ticket and previous.ticket[1] == endpoint(url):
            return fn(self, *args, **kwargs)
        with Request(url): return fn(self, *args, **kwargs)
    return wrapped


def session_request(fn):
    @functools.wraps(fn)
    def wrapped(self, url, *args, **kwargs):
        if monitor.mode == 'off': return fn(self, url, *args, **kwargs)
        @contextmanager
        def measured():
            with fn(self, url, *args, **kwargs) as session:
                with Request(url) as request:
                    try:
                        yield session
                    except BaseException as error:
                        code = getattr(error, 'code', None)
                        if isinstance(code, int): request.complete(code, getattr(session, 'nbytes', 0))
                        elif getattr(session,'status',None): request.complete(session.status,session.nbytes)
                        raise
                    else:
                        request.complete(session.status, session.nbytes)
        return measured()
    return wrapped


@contextmanager
def urlopen(req, **kwargs):
    """Transparent streaming download wrapper for self-update's urllib path."""
    if monitor.mode == 'off':
        with urllib.request.urlopen(req, **kwargs) as response: yield response
        return
    url = req.full_url if hasattr(req, 'full_url') else req
    previous = _request.get()
    context = nullcontext(previous) if previous is not None and previous.ticket and previous.ticket[1] == endpoint(url) else Request(url)
    with context as measured:
        try:
            with urllib.request.urlopen(req, **kwargs) as response:
                measured.headers()
                measured.status = getattr(response,'status',200)
                class Response:
                    def __getattr__(self,name): return getattr(response,name)
                    def read(self,*args,**kw):
                        data=response.read(*args,**kw)
                        measured.add_received(len(data or b''))
                        return data
                yield Response()
                measured.complete(getattr(response,'status',200),measured.received)
        except urllib.error.HTTPError as error:
            measured.complete(error.code,measured.received)
            raise


def request(transport, url, headers, timeout):
    if monitor.mode == 'off': return transport.request(url, headers, timeout)
    # Application request bytes only: exclude TLS, TCP and response headers.
    sent = len(urlsplit(url).path.encode()) + 16 + sum(len(str(k).encode()) + len(str(v).encode()) + 4 for k,v in headers.items())
    with Request(url, sent) as measured:
        status, rh, body = transport.request(url, headers, timeout)
        measured.complete(status, len(body or b''))
        return status, rh, body


def disk_read(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if monitor.mode == 'off': return fn(*args, **kwargs)
        started = time.perf_counter() if monitor.mode == 'full' else None
        try:
            data = fn(*args, **kwargs)
        except BaseException:
            monitor.record_read(0, (time.perf_counter()-started)*1000 if started is not None else None, failed=True)
            raise
        monitor.record_read(len(data or b''), (time.perf_counter()-started)*1000 if started is not None else None, miss=data is None)
        return data
    return wrapped


class View:
    """Merge process-local cumulative counters once; bounded rates/history."""
    def __init__(self):
        self.previous = None
        self.history = collections.deque(maxlen=120)
        self.volumes = []
        self.volume_at = -math.inf

    def snapshot(self, sources, mode, server=None):
        now = time.perf_counter()
        services, processes = {}, []
        disk = dict(readBytes=0, reads=0, misses=0, errors=0, readMs=0, timedReads=0,
                    maxReadMs=0, outputBytes=0, writesObserved=0)
        for source in sources:
            for key in disk:
                if key == 'maxReadMs': disk[key] = max(disk[key], source.get('disk', {}).get(key, 0))
                else: disk[key] += source.get('disk', {}).get(key, 0)
            pio = source.get('processIO')
            if mode == 'full' and pio and now-source.get('observed', 0) < 3:
                processes.append(dict(pio, pid=source['pid']))
            for row in source.get('services', []):
                name = row['endpoint']
                merged = services.setdefault(name, dict(endpoint=name, requests=0, responses=0, failures=0,
                    httpErrors=0, rateLimited=0, inflight=0, receivedBytes=0, sentBytes=0, responseMs=0,
                    responseSamples=0, requestMs=0, completed=0, lastSeen=0, samples=[], fresh=False, unfinished=0))
                fresh = now-source.get('observed',0) < 3
                merged['fresh'] = merged['fresh'] or fresh
                merged['inflight' if fresh else 'unfinished'] += row.get('inflight',0)
                for key in ('requests','responses','failures','httpErrors','rateLimited',
                            'receivedBytes','sentBytes','responseMs','responseSamples','requestMs','completed'):
                    merged[key] += row.get(key, 0)
                if row['lastSeen'] >= merged['lastSeen']:
                    merged.update(lastSeen=row['lastSeen'],lastStatus=row.get('lastStatus'),lastError=row.get('lastError'),lastResponseMs=row.get('lastResponseMs'))
                if mode == 'full': merged['samples'].extend((t,v) for t,v in row.get('samples', []) if now-t <= 60)
        rows = []
        for row in services.values():
            row['responseAvgMs'] = row['responseMs']/row['responseSamples'] if row['responseSamples'] else None
            row['requestAvgMs'] = row['requestMs']/row['completed'] if row['completed'] else None
            row['failureShare'] = row['failures']/max(1,row['responses']+row['failures'])
            row['ageS'] = max(0,now-row.pop('lastSeen'))
            row['health'] = ('telemetry stale' if not row.pop('fresh') else 'connection error' if row.get('lastError') else 'rate limited' if row.get('lastStatus') == 429
                             else 'service error' if (row.get('lastStatus') or 0) >= 500 else 'active' if row['inflight']
                             else 'idle' if row['ageS'] > 30 else 'reachable')
            values = row.pop('samples')
            if mode == 'full' and values:
                row['responseHistory'] = [dict(at=t,ms=v) for t,v in sorted(values)[-64:]]
                values=[v for _,v in values]
                values.sort()
                row['responseP95Ms'] = values[max(0,math.ceil(len(values)*0.95)-1)]
                avg=sum(values)/len(values)
                row['jitterMs'] = math.sqrt(sum(dnp.sq(v-avg) for v in values)/len(values))
            rows.append(row)
        rows.sort(key=lambda r:(-r['inflight'],-r['failures'],r['endpoint']))
        rx=sum(r['receivedBytes'] for r in rows);tx=sum(r['sentBytes'] for r in rows)
        rates=dict(rxBps=None,txBps=None,cacheReadBps=None,cacheOutputBps=None)
        if self.previous:
            at,old=self.previous;dt=max(0.001,now-at)
            for key,value in [('rxBps',rx),('txBps',tx),('cacheReadBps',disk['readBytes']),('cacheOutputBps',disk['outputBytes'])]:
                rates[key]=max(0,value-old[key])/dt
        self.previous=(now,dict(rxBps=rx,txBps=tx,cacheReadBps=disk['readBytes'],cacheOutputBps=disk['outputBytes']))
        record=dict(rates,at=now,intervalS=dt if self.history else None,
                    processReadBps=sum(p['readBps'] or 0 for p in processes) if any(p['readBps'] is not None for p in processes) else None,
                    processWriteBps=sum(p['writeBps'] or 0 for p in processes) if any(p['writeBps'] is not None for p in processes) else None)
        self.history.append(record)
        if server is not None and now-self.volume_at >= 5:
            volumes=[]
            for label,path in [('Cache',getattr(server,'cache_dir',None)),('Generated world',getattr(server,'out_dir',None))]:
                if path:
                    try:
                        value=shutil.disk_usage(path)
                        volumes.append(dict(label=label,path=path,totalBytes=value.total,usedBytes=value.used,freeBytes=value.free))
                    except OSError:
                        volumes.append(dict(label=label,path=path,unavailable=True))
            self.volumes,self.volume_at=volumes,now
        cache=getattr(server,'cache',None) if server is not None else None
        cache_usage=cache.message() if cache is not None else None
        return dict(mode=mode, observedAt=now, services=rows, disk=disk, processes=processes, rates=rates,
                    volumes=self.volumes,cache=cache_usage,history=list(self.history),
                    receivedBytes=rx,sentBytes=tx,omitted=sum(s.get('omitted',0) for s in sources))
