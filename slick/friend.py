import json
import math
import asyncio
import hashlib
import aiofiles
import base64
from tqdm import tqdm
from aiofile import AIOFile
from slick.connection import TorConnection, DirectConnection
from slick.logger import logger

file_chunk_size = 1_048_576
concurrency = 10


class Worker:
    def __init__(self, queue, fh, connection, size, path, bar):
        self.queue = queue
        self.fh = fh
        self.connection = connection
        self.size = size
        self.path = path
        self.bar = bar

    async def run(self):
        while self.queue.qsize() != 0:
            index = self.queue.get_nowait()
            logger.debug(f"worker got index {index} for {self.fh}")
            byte_range = (
                index * file_chunk_size,
                min(self.size, (index + 1) * file_chunk_size),
            )
            logger.debug(f"worker getting byte range {byte_range} for {index}")
            content = await self.connection.get_file(self.path, range=byte_range)
            await self.fh.write(content, offset=byte_range[0])
            logger.debug(f"worker done writing byte range {byte_range} for {index}")
            self.queue.task_done()
            self.bar.update(byte_range[1] - byte_range[0])


class Friend:
    @classmethod
    def read(cls, app, fh):
        data = json.load(fh)
        return Friend(
            app,
            onion=data["onion"],
            name=data["name"],
            cert=data["cert"],
            public_key=base64.b64decode(data["public_key"]),
        )

    def __init__(self, app, *, onion, name, cert, public_key):
        self.app = app
        self.onion = onion
        self.name = name
        self.cert = cert
        self.public_key = public_key
        self.direct_connection = DirectConnection(self.app, self)
        self.tor_connection = TorConnection(self.app, self)
        m = hashlib.sha256()
        m.update(self.cert.encode())
        self.digest = m.digest()
        loop = asyncio.get_event_loop()
        self.tor_connect_task = loop.create_task(self.tor_connection.connect())
        self.direct_connect_task = loop.create_task(self.direct_connection.connect())

    def write(self, fh):
        data = {
            "onion": self.onion,
            "name": self.name,
            "cert": self.cert,
            "public_key": base64.b64encode(self.public_key).decode(),
        }
        json.dump(data, fh)

    def __str__(self):
        return f"{self.name} -- {self.digest.hex()}"

    def __hash__(self):
        return hash(f"{self.name} {self.digest.hex()}")

    def active(self):
        return self.direct_connection.active or self.tor_connection.active

    def connection(self):
        if self.direct_connection.active:
            return self.direct_connection
        else:
            return self.tor_connection

    @property
    def nearby(self):
        return self.app.discovery.nearby_for_digest(self.digest)

    async def send(self, message):
        connection = self.connection()
        if not connection or not connection.active:
            logger.debug(f"can't send {connection}")
            return False
        else:
            return await connection.send(message)

    async def offer_file(self, path):
        connection = self.connection()
        if not connection or not connection.active:
            return False
        else:
            return await connection.offer_file(path)

    async def get_file(self, *, path, size, target):
        connection = self.connection()
        if not connection or not connection.active:
            logger.debug(f"cannot get connection {connection}")
            return False
        else:
            loop = asyncio.get_event_loop()
            queue = asyncio.Queue()
            chunk_count = math.ceil(size / file_chunk_size)

            for i in range(chunk_count):
                await queue.put(i)

            async with aiofiles.open(target, "wb") as fh:
                await fh.truncate(size)

            with tqdm(total=size, unit="B", unit_scale=True) as bar:
                async with AIOFile(target, "wb") as fh:
                    for i in range(concurrency):
                        worker = Worker(queue, fh, connection, size, path, bar)
                        loop.create_task(worker.run())

                    await queue.join()
                    await fh.fsync()
