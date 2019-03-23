"""Tests for the pyheos library."""
import asyncio
import pytest
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Union
from urllib.parse import parse_qsl, urlparse

from pyheos import const
from pyheos.connection import SEPARATOR, SEPARATOR_BYTES

FILE_IO_POOL = ThreadPoolExecutor()


async def get_fixture(file: str):
    """Load a fixtures file."""
    file_name = "tests/fixtures/{file}.json".format(file=file)

    def read_file():
        with open(file_name) as open_file:
            return open_file.read()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(FILE_IO_POOL, read_file)


class MockHeosDevice:
    """Define a mock heos device."""

    def __init__(self):
        """Init a new instance of the mock heos device."""
        self._server = None  # type: asyncio.AbstractServer
        self._started = False
        self.connections = []  # type: List[ConnectionLog]
        self._custom_handlers = defaultdict(list)
        self._matchers = []  # type: List[CommandMatcher]

    async def start(self):
        """Start the heos server."""
        self._started = True
        self._server = await asyncio.start_server(
            self._handle_connection, '127.0.0.1', const.CLI_PORT)

    async def stop(self):
        """Stop the heos server."""
        self._started = False
        self._server.close()
        await self._server.wait_closed()

    async def write_event(self, event: str):
        """Send an event through the event channel."""
        connection = next(conn for conn in self.connections
                          if conn.is_registered_for_events)
        await connection.write(event)

    def register_one_time(self, command: str, fixture: Union[str, Callable]):
        """Register fixture to command to use one time."""
        self._custom_handlers[command].append(fixture)

    def register_command(self, fixture, player_id, target_args=None, *,
                         command=None):
        """Create a callback fixture."""
        expected_command = command or fixture.replace(".", "/")

        async def callback(command, args):
            response = await get_fixture(fixture)
            assert command == expected_command
            assert args['pid'] == str(player_id)
            if target_args:
                for key, value in target_args.items():
                    assert args.get(key) == value, key
            return response

        self._custom_handlers[expected_command].append(callback)

    def register(self, command: str, args: dict, response: str):
        """Register a matcher."""
        self._matchers.append(CommandMatcher(command, args, response))

    async def _handle_connection(
            self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):

        log = ConnectionLog(writer)
        self.connections.append(log)

        while self._started:
            try:
                result = await reader.readuntil(SEPARATOR_BYTES)
            except asyncio.IncompleteReadError:
                # Occurs when the reader is being stopped
                break

            result = result.decode().rstrip(SEPARATOR)

            url_parts = urlparse(result)
            query = dict(parse_qsl(url_parts.query))

            command = url_parts.hostname + url_parts.path
            fixture_name = "{}.{}".format(url_parts.hostname,
                                          url_parts.path.lstrip('/'))
            response = None

            # Try matchers
            matcher = next((matcher for matcher in self._matchers
                            if matcher.is_match(command, query)), None)
            if matcher:
                test = await matcher.get_response()
                writer.write((test + SEPARATOR).encode())
                await writer.drain()
                continue

            # See if we have any custom handlers registered
            custom_fixtures = self._custom_handlers[command]
            if custom_fixtures:
                # use first one
                fixture = custom_fixtures.pop(0)
                if asyncio.iscoroutinefunction(fixture):
                    response = await fixture(command, query)
                elif isinstance(fixture, Callable):
                    response = fixture(command, query)
                else:
                    response = await get_fixture(fixture)
            elif command == 'system/register_for_change_events':
                enable = query["enable"]
                if enable == 'on':
                    log.is_registered_for_events = True
                response = (await get_fixture(fixture_name)).replace(
                    "{enable}", enable)
            elif command == 'player/get_players':
                response = await get_fixture(fixture_name)

            elif command in (
                    'player/get_play_state',
                    'player/get_now_playing_media',
                    'player/get_volume',
                    'player/get_mute',
                    'player/get_play_mode'):
                response = (await get_fixture(fixture_name)) \
                    .replace('{player_id}', query['pid']) \
                    .replace('{sequence}', query['sequence'])
            else:
                pytest.fail("Unrecognized command: " + result)

            log.commands[command].append(result)

            if isinstance(response, str):
                writer.write((response + SEPARATOR).encode())
                await writer.drain()
            else:
                for resp in response:
                    writer.write((resp + SEPARATOR).encode())
                    await writer.drain()

        self.connections.remove(log)


class CommandMatcher:
    """Define a command match response."""

    def __init__(self, command: str, args: dict, response: str):
        """Init the command response."""
        self.command = command
        self.args = args
        self._response = response

    def is_match(self, command, args):
        """Determine if the command matches the target."""
        if command != self.command:
            return False
        if self.args:
            for key, value in self.args.items():
                if not args[key] == value:
                    return False
        return True

    async def get_response(self):
        """Get the response body."""
        return await get_fixture(self._response)


class ConnectionLog:
    """Define a connection log."""

    def __init__(self, writer: asyncio.StreamWriter):
        """Initialize the connection log."""
        self._writer = writer
        self.is_registered_for_events = False
        self.commands = defaultdict(list)

    async def write(self, payload: str):
        """Write the payload to the stream."""
        data = (payload + SEPARATOR).encode()
        self._writer.write(data)
        await self._writer.drain()
