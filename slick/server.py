import os
import ssl
import uuid
import json
import hashlib
import asyncio
from aiohttp import web
from cryptography import x509
from cryptography.x509.oid import ExtensionOID
from cryptography.hazmat.backends import default_backend
from nacl.public import SealedBox, PublicKey

from slick.logger import logger
from slick.util import find_free_port
from slick.friend import Friend
from slick.bencode import Request


class FriendRequest:
    def __init__(self, app, *, cert_bytes, name, public_key, digest):
        self.app = app
        self.cert_bytes = cert_bytes
        self.name = name
        self.public_key = public_key
        self.digest = digest
        self.accepted_result = asyncio.Future()

    async def accepted(self):
        await self.accepted_result
        return self.accepted_result.result()

    @property
    def onion_service(self):
        cert = x509.load_pem_x509_certificate(self.cert_bytes, default_backend())
        ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
        hosts = ext.value.get_values_for_type(x509.DNSName)
        return hosts[0]

    async def add(self):
        self.accepted_result.set_result(True)
        friend = Friend(
            self.app,
            onion=self.onion_service,
            name=self.name,
            cert=self.cert_bytes.decode(),
            public_key=self.public_key,
        )
        logger.debug(f"adding {friend}")
        await self.app.friend_list.add(friend)
        logger.debug(f"done adding {friend}")

    @property
    def key(self):
        return f"{self.name} {self.digest.hex()}"

    def seal(self, data):
        sealed_box = SealedBox(PublicKey(self.public_key))
        return sealed_box.encrypt(data)


class BaseServer:
    def __init__(self, app):
        self.app = app
        self.runner = None

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()


class Message:
    def __init__(self, app, *, sender, content_type, data):
        self.sender = sender
        self.app = app
        self.content_type = content_type
        self.data = data

    def text(self):
        return self.data.decode()

    def json(self):
        return json.loads(self.text())

    def __str__(self):
        return f"{self.sender.name} {self.sender.onion[0:6]} -> {self.text()}"


class OfferedFile:
    def __init__(self, path):
        self.path = path
        self.friends = {}
        self.uuid = str(uuid.uuid4())

    def add(self, friend):
        self.friends[friend.digest] = friend

    def has_permission(self, friend):
        return friend.digest in self.friends


class TalkServer(BaseServer):
    def __init__(self, app):
        super().__init__(app)
        self.files = {}

    @property
    def _name(self):
        return "talk"

    async def start(self):
        await self.app.certificate.public_cert_bytes()
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(
            certfile=os.path.join(self.app.base, "server.crt"),
            keyfile=os.path.join(self.app.base, "server.key"),
        )
        for f in self.app.friend_list.friends():
            ssl_context.load_verify_locations(cadata=f.cert)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_REQUIRED

        self.web_app = web.Application()
        self.web_app.add_routes(
            [
                web.head("/", self.handle_head),
                web.post("/", self.handle_post),
                web.get("/f/{file_id}", self.handle_file),
            ]
        )

        self.runner = web.AppRunner(self.web_app)
        await self.runner.setup()
        self.site = web.TCPSite(
            self.runner,
            "0.0.0.0",
            await self.app.identity.port(),
            ssl_context=ssl_context,
        )
        await self.site.start()

    def offer_file(self, friend, path):
        abs_path = os.path.abspath(path)
        if abs_path not in self.files:
            offered_file = OfferedFile(abs_path)
            self.files[offered_file.uuid] = offered_file
        offered_file.add(friend)
        return f"/f/{offered_file.uuid}"

    async def restart(self):
        await self.stop()
        await self.start()

    async def handle_post(self, request):
        common_name = self.common_name(request)
        sender = self.app.friend_list.get_friend_for_onion(common_name)
        data = await request.read()
        content_type = request.content_type
        message = Message(self.app, sender=sender, content_type=content_type, data=data)
        await self.app.handle_incoming_message(message)
        return web.Response(status=201)

    async def handle_head(self, request):
        return web.Response(status=200)

    async def handle_file(self, request):
        file_id = request.match_info["file_id"]
        if file_id not in self.files:
            return web.Response(status=404)
        common_name = self.common_name(request)
        sender = self.app.friend_list.get_friend_for_onion(common_name)
        file = self.files[file_id]
        if file.has_permission(sender):
            return web.FileResponse(self.files[file_id].path)
        else:
            return web.Response(status=404)

    def common_name(self, request):
        san = request.transport._ssl_protocol._extra["peercert"]["subjectAltName"]
        return san[0][1]


class CertServer(BaseServer):
    def __init__(self, app):
        super().__init__(app)
        self.port_result = asyncio.Future()

    @property
    def _name(self):
        return "cert"

    async def start(self):
        port = find_free_port()
        app = web.Application()
        app.add_routes([web.post("/", self.handle_request)])

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()

        response = await self.app.tor.create_service({80: port})
        self.port_result.set_result(port)
        await self.app.discovery.set_cert_host(response.service_id)

    async def handle_request(self, request):
        logger.debug("handling friend request")
        encrypted_data = await request.read()
        data = self.app.identity.unseal(encrypted_data)
        payload = Request.decode(data)
        m = hashlib.sha256()
        m.update(payload["cert"])
        data_digest = m.digest()
        friend_request = FriendRequest(
            self.app,
            cert_bytes=payload["cert"],
            name=payload["name"],
            public_key=payload["public_key"],
            digest=data_digest,
        )
        self.app.handle_friend_request(friend_request)
        accepted = await friend_request.accepted()
        if accepted:
            return web.Response(
                content_type="application/octet-stream",
                body=friend_request.seal(await self.app.identity.greeting_payload()),
            )
        else:
            return web.Response(status=401)

    async def port(self):
        await self.port_result
        return self.port_result.result()
