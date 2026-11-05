"""Cross-process public Ondo GET budget; OS locks release on process exit."""
from contextlib import asynccontextmanager
import asyncio
import json
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit
from olbot.markets import _get_json


@asynccontextmanager
async def disk_lock(folder, timeout=90):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    stream=(folder/'PUBLIC_REST.lock').open('a+b')
    stream.seek(0,2)
    if stream.tell()==0:stream.write(b'0');stream.flush()
    acquired=False;deadline=time.monotonic()+timeout
    try:
        while not acquired:
            try:
                stream.seek(0)
                if __import__('os').name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                acquired=True
            except OSError:
                if time.monotonic()>deadline:raise TimeoutError('public REST lock busy')
                await asyncio.sleep(.05)
        yield
    finally:
        if acquired:
            stream.seek(0)
            if __import__('os').name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        stream.close()


async def public_get(url,coordination,*,owner=None,getter=None,spacing=1.1):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.netloc!='api.ondoperps.xyz' or parsed.path not in {
        '/v1/markets','/v1/perps/depth','/v1/perps/funding_rate_history'}:
        raise ValueError('public GET allowlist only')
    folder=Path(coordination);pause=folder/'PAUSE_REST'
    def paused():
        try:return pause.read_text(encoding='utf-8')!=owner
        except FileNotFoundError:return False
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        if paused():await asyncio.sleep(.1);continue
        async with disk_lock(folder):
            if paused():continue
            stamp=folder/'PUBLIC_REST_LAST.json'
            previous=json.loads(stamp.read_text(encoding='utf-8')) if stamp.exists() else {}
            # Wait from completed I/O, not pre-dispatch reservation: a delayed worker
            # thread could otherwise start just before the next process's request.
            completed=previous.get('completed_monotonic')
            if completed is not None and 0 <= time.monotonic()-completed:
                wait=max(0,spacing-(time.monotonic()-completed))
            elif previous:
                wait=spacing  # Old schema or a reboot: one full safe interval.
            else:
                wait=0
            await asyncio.sleep(wait)
            if paused():continue
            token=uuid.uuid4().hex;active=folder/'REST_ACTIVE'
            active.write_text(token,encoding='utf-8')
            request_ts=time.time()
            stamp.write_text(json.dumps({'request_ts':request_ts}),encoding='utf-8')
            with (folder/'PUBLIC_REST_REQUESTS.jsonl').open('a',encoding='utf-8') as audit:
                audit.write(json.dumps({'request_ts':request_ts,'pid':__import__('os').getpid(),'url':url})+'\n')
            try:
                task=asyncio.create_task(asyncio.to_thread(getter or _get_json,url))
                try:return await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task  # Keep the cross-process lock until the synchronous GET has ended.
                    raise
            finally:
                stamp.write_text(json.dumps({'request_ts':request_ts,
                    'completed_monotonic':time.monotonic(),'completed_ts':time.time()}),encoding='utf-8')
                if active.exists() and active.read_text(encoding='utf-8')==token:active.unlink()
    raise TimeoutError('public REST paused')
