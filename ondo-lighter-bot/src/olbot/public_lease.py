"""다른 공개 수집기의 REST와 직렬화. 키/인증/주문 없이 사용한다."""
from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
import time
import uuid


@asynccontextmanager
async def public_rest_lease(comparison, timeout=60):
    folder=Path(comparison)
    pause=folder/'PAUSE_REST'
    token='public-funding-'+uuid.uuid4().hex
    acquired=False
    deadline=time.monotonic()+timeout
    try:
        while not acquired:
            try:
                with pause.open('x',encoding='utf-8') as stream: stream.write(token)
                acquired=True
            except FileExistsError:
                if time.monotonic()>deadline: raise TimeoutError('public REST lease busy')
                await asyncio.sleep(.1)
        while (folder/'REST_ACTIVE').exists():
            if time.monotonic()>deadline: raise TimeoutError('public REST still active')
            await asyncio.sleep(.1)
        await asyncio.sleep(1.1)
        yield
    finally:
        if acquired:
            await asyncio.sleep(1.1)
            if pause.exists() and pause.read_text(encoding='utf-8')==token:
                pause.unlink()
