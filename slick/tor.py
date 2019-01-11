import os
import asyncio
from stem.control import Controller
import stem.process
from stem.util import log
from random import randint
from slick.logger import logger


def print_bootstrap_lines(line):
    logger.debug(line)


class ServiceResponse:
    def __init__(self, private_key: str, service_id: str):
        self.private_key = private_key
        self.service_id = service_id


class Tor:
    def __init__(self, app):
        self.app = app
        self.services = dict()
        self.tor_process = None
        self.socks_port_result = asyncio.Future()
        log.get_logger().level = log.logging_level(log.Runlevel.INFO)

    @property
    def _name(self):
        return "tor"

    async def start(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._start)

    def _start(self):
        try:
            self.data_directory = os.path.join(self.app.base, "tor")
            os.makedirs(self.data_directory, exist_ok=True)
            port = randint(9050, 9999)
            socks_port = port + 1
            self.tor_process = stem.process.launch_tor_with_config(
                take_ownership=True,
                init_msg_handler=print_bootstrap_lines,
                config={
                    "CookieAuthentication": "1",
                    "ControlPort": str(port),
                    "SocksPort": str(socks_port),
                    "DataDirectory": self.data_directory,
                },
            )

            self.controller = Controller.from_port(port=port)
            self.controller.authenticate()
            self.socks_port_result.set_result(socks_port)
        except Exception as e:
            self.socks_port_result.set_exception(e)

    async def stop(self):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._stop)

    async def socks_port(self):
        await self.socks_port_result
        return self.socks_port_result.result()

    def _stop(self):
        if self.tor_process:
            self.tor_process.kill()

    async def create_service(self, port) -> ServiceResponse:
        await self.socks_port_result
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._create_service, port)

    def _create_service(self, port) -> ServiceResponse:
        response = self.controller.create_ephemeral_hidden_service(
            port,
            key_type="NEW",
            key_content="ED25519-V3",
            await_publication=True,
            detached=True,
        )
        self.services[response.service_id] = response.private_key
        return ServiceResponse(response.private_key, response.service_id)

    async def add_service(self, key_content, port) -> ServiceResponse:
        await self.socks_port_result
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._add_service, key_content, port)

    def _add_service(self, key_content, port) -> ServiceResponse:
        response = self.controller.create_ephemeral_hidden_service(
            port,
            key_type="ED25519-V3",
            key_content=key_content,
            await_publication=True,
            detached=True,
        )
        self.services[response.service_id] = key_content
        return ServiceResponse(key_content, response.service_id)

    async def remove_service(self, service_id):
        await self.socks_port_result
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._remove_service, service_id)

    def _remove_service(self, service_id):
        self.controller.remove_ephemeral_hidden_service(service_id)
        del self.services[service_id]
