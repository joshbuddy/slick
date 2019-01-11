import os
from slick.friend import Friend
from slick.logger import logger


class FriendList:
    def __init__(self, app):
        self.app = app
        self.friend_dir = os.path.join(self.app.base, "friends")
        self._friends = []

    async def start(self):
        logger.debug("loading friend list")
        os.makedirs(self.friend_dir, exist_ok=True)
        friend_list = os.listdir(self.friend_dir)
        for f in friend_list:
            with open(os.path.join(self.friend_dir, f), "r") as fh:
                friend = Friend.read(self.app, fh)
                self._friends.append(friend)

    def has_digest(self, digest):
        for f in self._friends:
            if f.digest == digest:
                return True
        return False

    @property
    def _name(self):
        return "friend list"

    async def stop(self):
        pass

    def friends(self):
        return self._friends

    async def add(self, friend):
        if os.path.isfile(self.friend_path(friend)):
            return
        with open(self.friend_path(friend), "w") as fh:
            friend.write(fh)
            self._friends.append(friend)
            await self.app.talk_server.restart()

    async def remove(self, friend):
        self._friends.remove(friend)
        os.remove(self.friend_path(friend))

    def friend_path(self, friend):
        return os.path.join(self.friend_dir, f"{friend.name}-{friend.digest.hex()}")

    def get_friend_for_onion(self, onion):
        for f in self._friends:
            if f.onion == onion:
                return f
        raise Exception(f"could not find friend for {onion}")
