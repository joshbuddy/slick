import os
import asyncio
import logging
import tempfile
import shutil
from contextlib import asynccontextmanager
from enum import Enum

from slick.tor import Tor
from slick.certificate import Certificate
from slick.identity import Identity
from slick.friend_list import FriendList
from slick.discovery import Discovery
from slick.server import CertServer, TalkServer
from slick.logger import logger


class ServiceStatus(Enum):
    INITIALIZING = 1
    STARTED = 2
    ERRORED = 3
    STOPPING = 4
    STOPPED = 5


class App:
    def __init__(self, *, base, loop, message_handler, friend_handler):
        self.delete_at_exit = False

        self.base = base
        self.service_states = {}

        self.tor = Tor(self)
        self.certificate = Certificate(self)
        self.friend_list = FriendList(self)
        self.identity = Identity(self)
        self.cert_server = CertServer(self)
        self.discovery = Discovery(self, loop)
        self.talk_server = TalkServer(self)

        self.handle_incoming_message = message_handler
        self.handle_friend_request = friend_handler

        self.services = [
            self.tor,
            self.certificate,
            self.friend_list,
            self.identity,
            self.cert_server,
            self.discovery,
            self.talk_server,
        ]
        self.service_tasks = []

    def initialize(self):
        if self.base is None:
            self.base = tempfile.mkdtemp()
            self.delete_at_exit = True

        os.makedirs(self.base, exist_ok=True)

        f_handler = logging.FileHandler(os.path.join(self.base, "slick.log"))
        f_handler.setLevel(logging.DEBUG)
        f_format = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        f_handler.setFormatter(f_format)
        logger.addHandler(f_handler)

    @asynccontextmanager
    async def run(self):
        try:
            await self.start()
            yield self
        finally:
            await self.stop()

    async def start(self):
        logger.debug("Starting app")
        self.initialize()
        loop = asyncio.get_running_loop()
        for s in self.services:
            logger.debug(f"Starting {s._name}")
            self.service_tasks.append(loop.create_task(self._start_service(s)))

    async def stop(self):
        try:
            loop = asyncio.get_running_loop()
            logger.debug("Stopping app")
            for t in self.service_tasks:
                t.cancel()

            await asyncio.gather(
                *[loop.create_task(self._stop_service(s)) for s in self.services]
            )
        finally:
            if self.delete_at_exit:
                shutil.rmtree(self.base)

    async def handle_incoming_message(self, message):
        raise Exception("this method must be re-defined")

    def get_nearby(self):
        return self.discovery.nearby

    def offer_file(self, friend, path):
        return self.talk_server.offer_file(friend, path)

    async def _start_service(self, service):
        try:
            self.service_states[str(service._name)] = ServiceStatus.INITIALIZING
            await service.start()
            self.service_states[str(service._name)] = ServiceStatus.STARTED
        except Exception as e:
            self.service_states[str(service._name)] = ServiceStatus.ERRORED
            logger.exception(e)

    async def _stop_service(self, service):
        self.service_states[str(service._name)] = ServiceStatus.STOPPING
        await service.stop()
        self.service_states[str(service._name)] = ServiceStatus.STOPPED
