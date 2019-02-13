import asyncio
import base64
import os
import json
from nacl.public import PrivateKey, SealedBox
from slick.util import find_free_port
from slick.bencode import Request


class Identity:
    def __init__(self, app):
        self.app = app
        self.port_result = asyncio.Future()
        self.name_result = asyncio.Future()
        self.service_id_result = asyncio.Future()
        self.setup_name = None

    @property
    def _name(self):
        return "ident"

    def requires_setup(self):
        ident_path = os.path.join(self.app.base, "ident")
        return not os.path.isfile(ident_path)

    def set_name(self, name):
        self.setup_name = name

    async def start(self):
        port = find_free_port()
        ident_path = os.path.join(self.app.base, "ident")
        if os.path.isfile(ident_path):
            with open(ident_path, "r") as fh:
                identity_config = json.load(fh)
                private_key_bytes = base64.b64decode(identity_config["key"])
                self._setup_private_key(private_key_bytes)
                self.service_id_result.set_result(
                    identity_config["onion"]["service_id"]
                )
                await self.app.tor.add_service(
                    identity_config["onion"]["pk"], {443: port}
                )
                self.name_result.set_result(identity_config["name"])
        else:
            if not self.setup_name:
                raise Exception("no name set")
            private_key_bytes = PrivateKey.generate()._private_key
            self._setup_private_key(private_key_bytes)
            response = await self.app.tor.create_service({443: port})
            self.service_id_result.set_result(response.service_id)
            config = {
                "name": self.setup_name,
                "key": base64.b64encode(private_key_bytes).decode(),
                "onion": {
                    "pk": response.private_key,
                    "service_id": response.service_id,
                },
            }
            out = json.dumps(config)
            with open(ident_path, "w") as fh:
                fh.write(out)
            self.name_result.set_result(self.setup_name)
        self.port_result.set_result(port)

    async def stop(self):
        pass

    async def port(self) -> int:
        await self.port_result
        return self.port_result.result()

    async def name(self) -> str:
        await self.name_result
        return self.name_result.result()

    async def public_key_bytes(self) -> bytes:
        await self.name_result
        return self._public_key_bytes

    async def service_id(self) -> str:
        await self.service_id_result
        return self.service_id_result.result()

    async def service_host(self) -> str:
        service_id = await self.service_id()
        return f"{service_id}.onion"

    async def greeting_payload(self) -> bytes:
        cert_bytes = await self.app.certificate.public_cert_bytes()
        return Request.encode(
            {
                "cert": cert_bytes,
                "name": await self.app.identity.name(),
                "public_key": await self.app.identity.public_key_bytes(),
            }
        )

    def _setup_private_key(self, bytes):
        private_key = PrivateKey(bytes)
        self._public_key_bytes = private_key.public_key._public_key
        self._public_key_b64 = base64.b64encode(self._public_key_bytes)
        self.unseal_box = SealedBox(private_key)

    def unseal(self, encrypted_data):
        return self.unseal_box.decrypt(encrypted_data)
