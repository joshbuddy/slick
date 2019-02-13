import os
import ssl
import aiohttp
import asyncio
import filetype
import aiofiles
from datetime import datetime
from aiohttp_socks import SocksConnector
from slick.logger import logger
from slick.bencode import File


class BaseConnection:
    def __init__(self, app, friend):
        self.app = app
        self.friend = friend
        self.active = False
        self.running = True
        self.connect_task = None
        self.ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        self.ssl_context.load_cert_chain(
            certfile=os.path.join(self.app.base, "server.crt"),
            keyfile=os.path.join(self.app.base, "server.key"),
        )
        self.ssl_context.load_verify_locations(cadata=self.friend.cert)
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        self.pause_time = 0

    async def connect(self):
        try:
            while True:
                logger.debug(f"starting connect for {self}")
                self.active = False
                self.connect_task = asyncio.ensure_future(self._connect())
                await self.connect_task
        except asyncio.CancelledError:
            logger.debug("re-connecting...")
        except Exception as e:
            logger.exception(e)
            await asyncio.sleep(self.pause_time)
        finally:
            asyncio.create_task(self.connect())

    def restart_connection(self):
        if not self.connect_task:
            return
        logger.debug(f"restarting {self}")
        self.connect_task.cancel()

    async def ping(self):
        while self.running:
            start_time = datetime.now()
            try:
                logger.debug(f"pinging {self}")
                async with self.session.head(
                    f"https://{self.host}/", ssl=self.ssl_context, timeout=60
                ) as resp:
                    logger.debug(f"ping response from {self} {resp}")
                    self.active = True
            except Exception as e:
                logger.debug("error while pinging %s", e)
                self.active = False
            finally:
                end_time = datetime.now()
                sleep_time = max(
                    [self.pause_time - (end_time - start_time).total_seconds(), 0]
                )
                logger.debug("sleeping for %s", sleep_time)
                await asyncio.sleep(sleep_time)

    async def send(self, message):
        async with self.session.post(
            f"https://{self.host}/", ssl=self.ssl_context, data=message.encode()
        ) as resp:
            logger.debug("send response %s", resp)
            if resp.status == 201:
                return True
            else:
                logger.warning("got an unusual status response %s", resp)
                return False

    async def offer_file(self, path):
        abspath = os.path.abspath(path)
        name = os.path.basename(path)
        url = self.app.offer_file(self.friend, abspath)
        async with aiofiles.open(abspath, "rb") as fh:
            ft = filetype.match(await fh.read(261))
            mimetype = ft.mime if ft else "application/octet-stream"
            stat = os.stat(abspath)
            data = File.encode(
                {"url": url, "size": stat.st_size, "type": mimetype, "name": name}
            )
            async with self.session.post(
                f"https://{self.host}/",
                ssl=self.ssl_context,
                data=data,
                headers={"Content-Type": "x-slick/file"},
            ) as resp:
                logger.debug("send response %s", resp)

    async def get_file(self, path, range=None):
        url = f"https://{self.host}{path}"
        logger.debug("send response %s", url)
        headers = {}
        if range:
            headers["Range"] = f"bytes={range[0]}-{range[1]}"

        async with self.session.get(url, ssl=self.ssl_context, headers=headers) as resp:
            return await resp.content.read()


class TorConnection(BaseConnection):
    def __init__(self, app, friend):
        super().__init__(app, friend)
        self.host = friend.onion
        self.pause_time = 60

    async def _connect(self):
        try:
            socks_port = await self.app.tor.socks_port()
            conn = SocksConnector.from_url(
                f"socks5://127.0.0.1:{socks_port}", rdns=True
            )
            async with aiohttp.ClientSession(connector=conn) as session:
                self.session = session
                await self.ping()
        except Exception as e:
            logger.exception(e)
            raise e

    def __str__(self):
        return f"tor {self.host}"


class DirectConnection(BaseConnection):
    def __init__(self, app, friend):
        super().__init__(app, friend)
        self.pause_time = 5
        self.ping_task = None

    async def _connect(self):
        try:
            logger.debug("trying direct connect")
            if not self.friend.nearby:
                logger.debug("no nearby, sleeping")
                await asyncio.sleep(self.pause_time)
                return
            self.host = self.friend.nearby.direct_talk_ip_port
            logger.debug("doing a direct connect to %s", self.host)
            async with aiohttp.ClientSession() as session:
                self.session = session
                logger.debug("initiating ping for %s", self.host)
                await self.ping()
        except asyncio.CancelledError as e:
            raise e
        except Exception as e:
            logger.exception(e)
            raise e

    def __str__(self):
        return f"direct {self.friend.nearby}"
