import socket
import aiohttp
import asyncio
import hashlib
from typing import cast
from aiozeroconf import ServiceBrowser, ServiceStateChange, Zeroconf, ServiceInfo
from aiohttp_socks import SocksConnector
from ordered_set import OrderedSet
from nacl.public import SealedBox, PublicKey

from slick.server import FriendRequest
from slick.logger import logger
from slick.bencode import Request


class DigestMismatchError(Exception):
    pass


class Nearby:
    def __init__(
        self,
        app,
        *,
        name,
        host,
        cert_service_id,
        ip,
        digest,
        public_key,
        cert_port,
        talk_port,
    ):
        self.app = app
        self.name = name
        self.host = host
        self.cert_service_id = cert_service_id
        self.ip = ip
        self.digest = digest
        self.public_key = public_key
        self.cert_port = cert_port
        self.talk_port = talk_port
        self.friend = None

    async def add(self):
        cert_bytes = await self.app.certificate.public_cert_bytes()
        greeting_payload = await self.app.identity.greeting_payload()
        sealed_greeting = self.seal(greeting_payload)

        added = False
        try:
            added = await self.attempt_add_direct(
                cert_bytes, greeting_payload, sealed_greeting
            )
        except Exception as e:
            logger.debug("cannot make a direct connection")
            logger.exception(e)
            added = await self.attempt_add_tor(
                cert_bytes, greeting_payload, sealed_greeting
            )
        return added

    async def attempt_add_direct(self, cert_bytes, greeting_payload, sealed_greeting):
        # todo, i should start with the local one, give up if i can't connect within a second?
        async with aiohttp.ClientSession(conn_timeout=1) as session:
            async with session.post(
                f"http://{self.ip}:{self.cert_port}/", data=sealed_greeting
            ) as resp:
                await self.process_add_response(resp)

    async def attempt_add_tor(self, cert_bytes, greeting_payload, sealed_greeting):
        socks_port = await self.app.tor.socks_port()
        conn = SocksConnector.from_url(f"socks5://127.0.0.1:{socks_port}", rdns=True)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.post(
                f"http://{self.cert_service_id}.onion/", data=sealed_greeting
            ) as resp:
                await self.process_add_response(resp)

    def seal(self, data):
        sealed_box = SealedBox(PublicKey(self.public_key))
        return sealed_box.encrypt(data)

    async def process_add_response(self, resp):
        if resp.status == 200:
            encrypted_data = await resp.read()
            data = self.app.identity.unseal(encrypted_data)
            friend_response = Request.decode(data)
            m = hashlib.sha256()
            m.update(friend_response["cert"])
            if m.digest() != self.digest:
                raise DigestMismatchError(f"expected {self.digest} got {m.digest()}")

            friend_request = FriendRequest(
                self.app,
                cert_bytes=friend_response["cert"],
                name=friend_response["name"],
                public_key=friend_response["public_key"],
                digest=self.digest,
            )
            await friend_request.add()
            return True
        else:
            logger.debug("nope on adding")
            return False

    def __str__(self):
        return f"{self.name} -- {self.digest.hex()} {self.ip} {self.talk_port}"

    def __eq__(self, other):
        return str(self) == str(other)

    def __hash__(self):
        return self.digest.__hash__()

    @property
    def key(self):
        return f"{self.name} {self.digest.hex()}"

    @property
    def direct_talk_ip_port(self):
        return f"{self.ip}:{self.talk_port}"


class Discovery:
    def __init__(self, app, loop):
        self.app = app
        self.restart_queue = asyncio.Queue()
        self.zeroconf = Zeroconf(loop)
        self.nearby = OrderedSet()
        self.info = None
        self.cert_host = None

    @property
    def _name(self):
        return "discovery"

    async def start(self):
        logger.debug(f"socket.getfqdn(): {socket.getfqdn()}")
        logger.debug(
            f"socket.gethostbyname(socket.getfqdn()): {socket.gethostbyname_ex(socket.gethostname())[2][0]}"
        )
        fqdn = socket.gethostbyname_ex(socket.gethostname())[2][0]
        port = await self.app.identity.port()
        cert_digest = await self.app.certificate.digest()
        name = await self.app.identity.name()
        cert_port = await self.app.cert_server.port()
        public_key_bytes = await self.app.identity.public_key_bytes()

        logger.debug(f"cert port is being broadcast as {cert_port}")

        properties = {"d": cert_digest, "pk": public_key_bytes, "cp": str(cert_port)}
        logger.debug(f"properties being broadcast are {properties}")

        if self.cert_host:
            properties["cs"] = self.cert_host

        self.info = ServiceInfo(
            "_slick._tcp.local.",
            name=f"{name}.{cert_digest.hex()[0:6]}._slick._tcp.local.",
            address=socket.inet_aton(fqdn),
            port=port,
            properties=properties,
        )
        self.browser = ServiceBrowser(
            self.zeroconf, "_slick._tcp.local.", handlers=[self.on_service_state_change]
        )
        await self.zeroconf.register_service(self.info)

        loop = asyncio.get_event_loop()
        self.restart_worker_task = loop.create_task(self.run_restart_worker())

    async def set_cert_host(self, cert_host):
        self.cert_host = cert_host
        await self.restart_queue.put(True)

    async def stop(self):
        try:
            if self.info:
                await self.zeroconf.unregister_service(self.info)
        except Exception as e:
            logger.debug("Key error %s", e)

    async def run_restart_worker(self):
        while True:
            await self.restart_queue.get()
            await self.restart()

    async def restart(self):
        await self.stop()
        await self.start()

    def on_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        asyncio.ensure_future(
            self.process_service_state_change(
                zeroconf, service_type, name, state_change
            )
        )

    def nearby_for_digest(self, digest):
        for n in self.nearby:
            if digest == n.digest:
                return n

    async def process_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is ServiceStateChange.Added:
            info = await zeroconf.get_service_info(service_type, name)
            if info:
                cert_service_id = (
                    info.properties[b"cs"].decode()
                    if b"cs" in info.properties
                    else None
                )
                cert_digest = await self.app.certificate.digest()
                parts = info.server.split(".")
                logger.debug(f"got a service with {info.properties}")

                nearby = Nearby(
                    self.app,
                    host=name,
                    name=parts[0],
                    cert_service_id=cert_service_id,
                    ip=socket.inet_ntoa(cast(bytes, info.address)),
                    digest=info.properties[b"d"],
                    public_key=info.properties[b"pk"],
                    cert_port=int(info.properties[b"cp"]),
                    talk_port=info.port,
                )
                if nearby.digest == cert_digest:
                    return

                self.nearby.add(nearby)
            else:
                logger.warning("no properties for %s", name)
        elif state_change is ServiceStateChange.Removed:
            for i in range(len(self.nearby)):
                if self.nearby[i].host == name:
                    self.nearby.discard(self.nearby[i])
        else:
            logger.warning("strange state")
