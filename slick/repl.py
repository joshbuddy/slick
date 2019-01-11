import re
import os
import glob
import click
import asyncio
import humanize
from slick.app import App, ServiceStatus
from os.path import expanduser
from prompt_toolkit import PromptSession
from prompt_toolkit.eventloop import use_asyncio_event_loop
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.validation import Validator, ValidationError
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit import print_formatted_text
from slick.server import FriendRequest
from slick.discovery import Nearby
from slick.bencode import File

potential_commands = [
    "/send ",
    "/get ",
    "/end ",
    "/ls ",
    "/list ",
    "/talk ",
    "/add ",
    "/remove ",
    "/help ",
    "/info ",
]

bindings = KeyBindings()


@bindings.add("c-w")
def back_a_word(event):
    buff = event.app.current_buffer
    matches = list(re.finditer(r"([ /])", buff.text))
    if matches:
        match = matches[-1]
        new_text = buff.text[: match.end() - 1]
        buff.text = new_text
        buff._set_cursor_position(match.end() - 1)


class CommandValidator(Validator):
    def validate(self, document):
        text = document.text
        if text.startswith("/send "):
            path = text[5:].strip()
            if not os.path.isfile(path):
                if os.path.isdir(path):
                    raise ValidationError(
                        message="Cannot be a directory",
                        cursor_position=document.cursor_position,
                    )
                else:
                    raise ValidationError(
                        message="Must be a file",
                        cursor_position=document.cursor_position,
                    )


class CommandCompleter(Completer):
    def __init__(self, repl):
        self.repl = repl

    def get_completions(self, document, complete_event):
        if not document.text.startswith("/"):
            return
        elif document.text.startswith("/send "):
            potential_path = document.text[6:].strip()
            if os.path.isdir(potential_path):
                search = f"{expanduser(potential_path)}/*"
            else:
                search = f"{expanduser(potential_path)}*"

            files = glob.glob(search)
            for f in files:
                yield Completion(f, start_position=-document.cursor_position + 6)
        elif document.text.startswith("/get "):
            potential_get = document.text[5:].strip()
            for i in range(len(self.repl.files)):
                if str(i).startswith(potential_get):
                    yield Completion(
                        f"/get {i}", start_position=-document.cursor_position + 4
                    )
        else:
            for c in potential_commands:
                if c.startswith(document.text):
                    yield Completion(c, start_position=-document.cursor_position)


class Repl:
    def __init__(self, *, base, loop):
        use_asyncio_event_loop(loop)

        self.app = App(
            base=base,
            loop=loop,
            message_handler=self.handle_incoming_message,
            friend_handler=self.handle_friend_request,
        )
        self.files = []
        self.active_friend = None
        self.prompt_session = PromptSession()
        self.online_friends = []
        self.offline_friends = []
        self.count = 0
        self.addable_entities = {}
        self.friend_request_count = 0
        loop.create_task(self.run_update())

    async def run(self):
        self.help_text = HTML(
            """
<b>/list | /ls</b>        show active friends and nearby people
<b>/add    [subject]</b>  add a person
<b>/remove [subject]</b>  add a person
<b>/talk   [subject]</b>  talk to someone
<b>/end</b>               stop talking to someone
<b>/quit</b>              quit the program
<b>/send</b>              send a file
<b>/get</b>               get a file
<b>/info</b>              print status information

"""
        )
        if self.app.identity.requires_setup():
            name = (
                await self.prompt_session.prompt(
                    "Please enter your name > ", async_=True
                )
            ).strip()
            self.app.identity.set_name(name)

        async with self.app.run():
            await self.update()
            try:
                self.continue_running = True
                while self.continue_running:
                    answer = (
                        await self.prompt_session.prompt(
                            HTML(self.generate_prompt()),
                            async_=True,
                            completer=CommandCompleter(self),
                            validator=CommandValidator(),
                            key_bindings=bindings,
                            bottom_toolbar=self.generate_bottom,
                            refresh_interval=0.5,
                        )
                    ).strip()
                    if answer == "":
                        continue
                    elif answer.startswith("/"):
                        if answer == "/end":
                            await self.end_conversation()
                        elif answer == "/help":
                            print_formatted_text(self.help_text)
                        elif answer.startswith("/send"):
                            await self.send_file(answer[5:].strip())
                        elif answer.startswith("/get"):
                            await self.get_file(answer[4:].strip())
                        elif answer.startswith("/list") or answer.startswith("/ls"):
                            await self.list()
                        elif answer.startswith("/add"):
                            await self.add(answer[4:].strip())
                        elif answer.startswith("/remove"):
                            await self.remove(answer[7:].strip())
                        elif answer.startswith("/talk"):
                            await self.talk(answer[5:].strip())
                        elif answer.startswith("/info"):
                            await self.info()
                        else:
                            print_formatted_text(self.help_text)
                            self.prompt_session.app.invalidate()
                    else:
                        if self.active_friend:
                            if not await self.active_friend.send(answer):
                                print(f"cannot reach {self.active_friend.name}")
                        else:
                            print_formatted_text(
                                HTML(
                                    "You're not currently talking to anyone\n\nUse <b>/talk [subject]</b> to talk to someone"
                                )
                            )
                            print_formatted_text(self.help_text)

            except KeyboardInterrupt:
                print("stopping...")

    async def handle_incoming_message(self, message):
        if message.content_type == "x-slick/file":
            file = File.decode(message.data)
            file["friend"] = message.sender
            self.files.append(file)
            print_formatted_text(
                HTML(
                    f"new file available #{len(self.files) - 1}\n<b>{file['name']}</b>"
                )
            )
            print_formatted_text(HTML(f"  from: <b>{file['friend'].name}</b>"))
            print_formatted_text(HTML(f"  {file['size']} bytes ({file['type']})"))
        else:
            print_formatted_text(
                HTML(f"<gray>{message.sender.name}</gray> {message.text()}")
            )
        self.prompt_session.app.invalidate()

    def handle_friend_request(self, friend_request):
        self.addable_entities[friend_request.key] = friend_request
        self.friend_request_count += 1

    async def end_conversation(self):
        self.active_friend = None

    async def talk(self, subject):
        await self.update()
        if subject == "":
            print("i need a person to talk to")
        else:
            matches = list(
                filter(lambda f: f.name.startswith(subject), self.online_friends)
            )
            if len(matches) == 0:
                print("no one matches that name")
            elif len(matches) > 1:
                print("too many match that name")
            else:
                self.active_friend = matches[0]

    async def list(self):
        await self.update()
        print_formatted_text(HTML("<violet>Online</violet>"))
        if not self.online_friends:
            print_formatted_text(HTML("  <i>None</i>"))
        else:
            for f in self.online_friends:
                print_formatted_text(
                    HTML(f"  {f.name} <gray>{f.digest.hex()[0:6]}</gray>")
                )

        print_formatted_text(HTML("<violet>Offline</violet>"))
        if not self.offline_friends:
            print_formatted_text(HTML("  <i>None</i>"))
        else:
            for f in self.offline_friends:
                print_formatted_text(
                    HTML(f"  {f.name} <gray>{f.digest.hex()[0:6]}</gray>")
                )

        requests = list(
            filter(
                lambda e: isinstance(e, FriendRequest), self.addable_entities.values()
            )
        )
        print_formatted_text(HTML("<violet>Requests</violet>"))
        if not requests:
            print_formatted_text(HTML("  <i>None</i>"))
        else:
            for r in requests:
                line = f"  {r.name} <gray>{r.digest.hex()[0:6]}</gray>"
                print_formatted_text(HTML(line))

        nearbys = list(
            filter(lambda e: isinstance(e, Nearby), self.addable_entities.values())
        )
        print_formatted_text(HTML("<violet>Nearby</violet>"))
        if not nearbys:
            print_formatted_text(HTML("  <i>None</i>"))
        else:
            for n in nearbys:
                line = f"  {n.name} <gray>{n.digest.hex()[0:6]}</gray>"
                print_formatted_text(HTML(line))

        print_formatted_text(HTML("<violet>Files</violet>"))
        if not self.files:
            print_formatted_text(HTML("  <i>None</i>"))
        else:
            for file_index in range(len(self.files)):
                file = self.files[file_index]
                line = f"  <b>#{file_index}</b>: <b>{file['name']}</b> type: <gray>{file['type']}</gray> size: <gray>{humanize.naturalsize(file['size'])}</gray> from <b>{file['friend'].name}</b>"
                print_formatted_text(HTML(line))

    async def add(self, name):
        await self.update()
        matches = list(
            filter(lambda k: k.startswith(name), self.addable_entities.keys())
        )
        if len(matches) == 0:
            print("no one matches that name")
        elif len(matches) > 1:
            print("too many match that name")
        else:
            match = self.addable_entities[matches[0]]
            name = await self.app.identity.name()
            digest = await self.app.certificate.digest()

            other_name = match.name
            other_digest = match.digest
            print(f"> {name} {digest.hex()}")
            print(f"> {other_name} {other_digest.hex()}")

            await match.add()

            del self.addable_entities[matches[0]]

            if isinstance(match, FriendRequest):
                self.friend_request_count -= 1

    async def remove(self, name):
        matches = list(
            filter(
                lambda f: f.__str__().startswith(name), self.app.friend_list.friends()
            )
        )
        if len(matches) == 0:
            print("no one matches that name")
        elif len(matches) > 1:
            print("too many match that name")
        else:
            match = matches[0]
            await self.app.friend_list.remove(match)
            print(f"Removed {match}")

    async def update(self):
        self.nearby = list(
            filter(
                lambda n: not self.app.friend_list.has_digest(n.digest),
                self.app.get_nearby(),
            )
        )

        friends = self.app.friend_list.friends()
        self.online_friends = list(filter(lambda f: f.active(), friends))
        self.offline_friends = list(filter(lambda f: not f.active(), friends))

        for n in self.nearby:
            if n.key not in self.addable_entities:
                self.addable_entities[n.key] = n

    def generate_prompt(self):
        return (
            "> "
            if not self.active_friend
            else f"[<ansired>{self.active_friend.name}</ansired>] &gt; "
        )

    async def send_file(self, path):
        if not self.active_friend:
            print("cannot send a file, not talking to anyone")
        else:
            await self.active_friend.offer_file(path)

    async def get_file(self, index_str):
        try:
            index = int(index_str)
            file = self.files[index]
            x = 0
            target = os.path.basename(file["name"])
            while os.path.isfile(target):
                x = x + 1
                target = f"{os.path.basename(file['name'])}.{x}"
            print(
                f"Writing {file['name']} ({humanize.naturalsize(file['size'])}) to {target}"
            )
            sender = file["friend"]
            await sender.get_file(path=file["url"], size=file["size"], target=target)
        except ValueError:
            print(f"Can't parse `{index_str}' as an integer")
        except IndexError:
            print(f"Can't get a file for `{index_str}'")

    async def info(self):
        for k, v in self.app.service_states.items():
            print(f"{k}: {v}")

    async def run_update(self):
        while True:
            await self.update()
            await asyncio.sleep(1)

    def generate_bottom(self):
        text = ""
        bad_state = 0
        loading_state = 0
        started_state = 0
        service_count = len(self.app.service_states.items())
        for k, v in self.app.service_states.items():
            if v == ServiceStatus.INITIALIZING:
                loading_state += 1
            elif v == ServiceStatus.STARTED:
                started_state += 1
            else:
                bad_state += 1

        if started_state == len(self.app.service_states):
            text += "<ansigreen>loaded</ansigreen>"
        elif bad_state != 0:
            text += "<ansired>error</ansired>"
        else:
            bar = "=" * (service_count - loading_state - 1)
            text += f"<seagreen>{bar}&gt;</seagreen>"

        near_count = len(self.nearby)
        online_friend_count = len(self.online_friends)
        offline_friend_count = len(self.offline_friends)

        if self.friend_request_count:
            text += f" [{self.friend_request_count} friend requests]"

        text += f" online {online_friend_count} | offline {offline_friend_count} | nearby {near_count}"
        return HTML(text)


@click.command()
@click.option("--base", default=expanduser("~/.slick"))
@click.option("--anonymous/--no-anonymous", default=False)
@click.version_option()
def run(base, anonymous):
    if anonymous:
        base = None
    loop = asyncio.get_event_loop()
    repl = Repl(base=base, loop=loop)
    loop.run_until_complete(repl.run())
    loop.close()


if __name__ == "__main__":
    run()
