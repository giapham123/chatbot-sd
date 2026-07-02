"""Async SocketCluster WebSocket client (ported from ai-agent)."""
import json
import asyncio
import websockets
import importlib
import logging

logger = logging.getLogger(__name__)


def _ws_is_open(ws) -> bool:
    if ws is None:
        return False
    if hasattr(ws, "state"):
        from websockets.connection import State
        return ws.state == State.OPEN
    return not getattr(ws, "closed", True)


try:
    Emitter = importlib.import_module(".Emitter", package="socketclusterclient")
except ImportError:
    class SimpleEmitter:
        def __init__(self):
            self._events = {}

        def on(self, event, listener):
            self._events.setdefault(event, []).append(listener)

        def off(self, event, listener=None):
            if event in self._events:
                if listener:
                    self._events[event] = [l for l in self._events[event] if l != listener]
                else:
                    self._events[event] = []

        def emit(self, event, *args):
            for listener in self._events.get(event, []):
                listener(*args)

        def execute(self, event, data):
            self.emit(event, data)

        def haseventack(self, event):
            return f"{event}_ack" in self._events

        def executeack(self, event, data, ack):
            self.emit(f"{event}_ack", data, ack)

    class emitter:
        pass
    emitter = SimpleEmitter
    Emitter = type("Emitter", (), {"emitter": emitter})()


try:
    Parser = importlib.import_module(".Parser", package="socketclusterclient")
except ImportError:
    class SimpleParser:
        @staticmethod
        def parse(dataobject, rid, cid, event):
            if event == "#setAuthToken":
                return 4
            elif event == "#removeAuthToken":
                return 3
            elif event == "#publish":
                return 2
            elif rid == "" and cid != "":
                if event == "#handshake":
                    return 1
                else:
                    return 5
            else:
                return 0
    Parser = SimpleParser()


class AsyncSocketCluster(Emitter.emitter):
    def enablelogger(self, enabled):
        pass

    def getlogger(self):
        return logger

    async def emitack(self, event, data, ack):
        emitobject = {"event": event, "data": data, "cid": self.getandincrement()}
        if _ws_is_open(self.ws):
            await self.ws.send(json.dumps(emitobject, sort_keys=True))
            self.acks[self.cnt] = [event, ack]

    async def emit(self, event, data):
        emitobject = {"event": event, "data": data}
        if _ws_is_open(self.ws):
            await self.ws.send(json.dumps(emitobject, sort_keys=True))

    async def subscribe(self, channel):
        subscribeobject = {
            "event": "#subscribe",
            "data": {"channel": channel},
            "cid": self.getandincrement(),
        }
        if _ws_is_open(self.ws):
            await self.ws.send(json.dumps(subscribeobject, sort_keys=True))
            if channel not in self.channels:
                self.channels.append(channel)

    async def subscribeack(self, channel, ack):
        subscribeobject = {
            "event": "#subscribe",
            "data": {"channel": channel},
            "cid": self.getandincrement(),
        }
        if _ws_is_open(self.ws):
            await self.ws.send(json.dumps(subscribeobject, sort_keys=True))
            if channel not in self.channels:
                self.channels.append(channel)
            self.acks[self.cnt] = [channel, ack]

    async def unsubscribe(self, channel):
        subscribeobject = {
            "event": "#unsubscribe",
            "data": channel,
            "cid": self.getandincrement(),
        }
        if _ws_is_open(self.ws):
            await self.ws.send(json.dumps(subscribeobject, sort_keys=True))
            if channel in self.channels:
                self.channels.remove(channel)

    async def publish(self, channel, data):
        publishobject = {
            "event": "#publish",
            "data": {"channel": channel, "data": data},
            "cid": self.getandincrement(),
        }
        if _ws_is_open(self.ws):
            await self.ws.send(json.dumps(publishobject, sort_keys=True))

    async def publishack(self, channel, data, ack):
        publishobject = {
            "event": "#publish",
            "data": {"channel": channel, **data},
            "cid": self.getandincrement(),
        }
        if _ws_is_open(self.ws):
            await self.ws.send(json.dumps(publishobject, sort_keys=True))
            self.acks[self.cnt] = [channel, ack]

    def getsubscribedchannels(self):
        return self.channels

    async def subscribechannels(self):
        for channel in self.channels:
            await self.subscribe(channel)

    def create_ack(self, cid):
        ws = self.ws

        async def message_ack(error, data):
            ackobject = {"error": error, "data": data, "rid": cid}
            if _ws_is_open(ws):
                await ws.send(json.dumps(ackobject, sort_keys=True))

        return message_ack

    class BlankDict(dict):
        def __missing__(self, key):
            return ""

    async def on_message(self, message):
        if message == "":
            if _ws_is_open(self.ws):
                await self.ws.send("")
        else:
            try:
                mainobject = json.loads(message, object_hook=self.BlankDict)
                dataobject = mainobject.get("data", {})
                rid = mainobject.get("rid", "")
                cid = mainobject.get("cid", "")
                event = mainobject.get("event", "")

                result = Parser.parse(dataobject, rid, cid, event)

                if result == 1:
                    await self.subscribechannels()
                    if self.on_authentication is not None:
                        self.id = dataobject.get("id", "")
                        if asyncio.iscoroutinefunction(self.on_authentication):
                            await self.on_authentication(self, dataobject.get("isAuthenticated", False))
                        else:
                            self.on_authentication(self, dataobject.get("isAuthenticated", False))

                elif result == 2:
                    channel = dataobject.get("channel", "")
                    data = dataobject.get("data", {})
                    if channel in self.channel_listeners:
                        listener = self.channel_listeners[channel]
                        try:
                            if asyncio.iscoroutinefunction(listener):
                                await listener(channel, data)
                            else:
                                listener(channel, data)
                        except Exception as e:
                            logger.error("Error in channel listener for %s: %s", channel, e)
                    if hasattr(self, "execute") and callable(self.execute):
                        if asyncio.iscoroutinefunction(self.execute):
                            await self.execute(channel, data)
                        else:
                            self.execute(channel, data)

                elif result == 3:
                    self.authToken = None

                elif result == 4:
                    if self.on_set_authentication is not None:
                        token = dataobject.get("token", "")
                        if asyncio.iscoroutinefunction(self.on_set_authentication):
                            await self.on_set_authentication(self, token)
                        else:
                            self.on_set_authentication(self, token)

                elif result == 5:
                    if hasattr(self, "haseventack") and callable(self.haseventack) and self.haseventack(event):
                        if hasattr(self, "executeack") and callable(self.executeack):
                            if asyncio.iscoroutinefunction(self.executeack):
                                await self.executeack(event, dataobject, self.create_ack(cid))
                            else:
                                self.executeack(event, dataobject, self.create_ack(cid))
                    else:
                        if hasattr(self, "execute") and callable(self.execute):
                            if asyncio.iscoroutinefunction(self.execute):
                                await self.execute(event, dataobject)
                            else:
                                self.execute(event, dataobject)
                else:
                    if rid in self.acks:
                        ack_tuple = self.acks.pop(rid, None)
                        if ack_tuple is not None:
                            ack_func = ack_tuple[1]
                            error = mainobject.get("error", None)
                            data = mainobject.get("data", None)
                            if asyncio.iscoroutinefunction(ack_func):
                                await ack_func(ack_tuple[0], error, data)
                            else:
                                ack_func(ack_tuple[0], error, data)

            except json.JSONDecodeError as e:
                logger.error("Failed to parse WS message: %s", e)
            except Exception as e:
                logger.error("Error processing WS message: %s", e)

    async def on_error(self, error):
        logger.error("WebSocket error: %s", error)
        if self.on_connect_error is not None:
            if asyncio.iscoroutinefunction(self.on_connect_error):
                await self.on_connect_error(self, error)
            else:
                self.on_connect_error(self, error)

    async def on_close(self):
        if self.on_disconnected is not None:
            if asyncio.iscoroutinefunction(self.on_disconnected):
                await self.on_disconnected(self)
            else:
                self.on_disconnected(self)
        if self.enable_reconnection and not self._disconnecting:
            await self.reconnect()

    def getandincrement(self):
        self.cnt += 1
        return self.cnt

    def resetvalue(self):
        self.cnt = 0

    async def on_open(self):
        self.resetvalue()
        handshakeobject = {
            "event": "#handshake",
            "data": {"authToken": self.authToken},
            "cid": self.getandincrement(),
        }
        if _ws_is_open(self.ws):
            await self.ws.send(json.dumps(handshakeobject, sort_keys=True))
        if self.on_connected is not None:
            if asyncio.iscoroutinefunction(self.on_connected):
                await self.on_connected(self)
            else:
                self.on_connected(self)

    def setAuthtoken(self, token):
        self.authToken = str(token) if token is not None else None

    def getAuthtoken(self):
        return self.authToken

    def __init__(self, url):
        super().__init__()
        self.id = ""
        self.cnt = 0
        self.authToken = None
        self.url = url
        self.acks = {}
        self.channels = []
        self.channel_listeners = {}
        self.enable_reconnection = False
        self.delay = 3
        self.ws = None
        self._connection_task = None
        self._disconnecting = False
        self.on_connected = None
        self.on_disconnected = None
        self.on_connect_error = None
        self.on_set_authentication = None
        self.on_authentication = None

    def _is_connected(self) -> bool:
        return _ws_is_open(self.ws)

    async def connect(self, ssl=None, extra_headers=None):
        try:
            self._disconnecting = False
            if ssl is None and self.url.startswith("wss://"):
                import ssl as ssl_module
                ssl = ssl_module.create_default_context()
            connect_kwargs = {"ping_interval": 20, "ping_timeout": 10}
            if ssl is not None:
                connect_kwargs["ssl"] = ssl
            if extra_headers is not None:
                connect_kwargs["extra_headers"] = extra_headers
            self.ws = await websockets.connect(self.url, **connect_kwargs)
            await self.on_open()
            self._connection_task = asyncio.create_task(self._message_loop())
            return self._connection_task
        except Exception as e:
            logger.error("WS connection failed: %s", e)
            await self.on_error(e)
            raise

    async def _message_loop(self):
        try:
            async for message in self.ws:
                await self.on_message(message)
        except websockets.exceptions.ConnectionClosed:
            await self.on_close()
        except Exception as e:
            logger.error("WS message loop error: %s", e)
            await self.on_error(e)
            await self.on_close()

    def setBasicListener(self, on_connected, on_disconnected, on_connect_error):
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_connect_error = on_connect_error

    async def reconnect(self):
        if not self.enable_reconnection or self._disconnecting:
            return
        await asyncio.sleep(self.delay)
        if not self._disconnecting:
            try:
                await self.connect()
            except Exception as e:
                logger.error("WS reconnection failed: %s", e)
                if self.enable_reconnection and not self._disconnecting:
                    await self.reconnect()

    def setdelay(self, delay):
        self.delay = delay

    def setreconnection(self, enable):
        self.enable_reconnection = enable

    def setAuthenticationListener(self, on_set_authentication, on_authentication):
        self.on_set_authentication = on_set_authentication
        self.on_authentication = on_authentication

    async def disconnect(self):
        self._disconnecting = True
        self.enable_reconnection = False
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass
        if _ws_is_open(self.ws):
            await self.ws.close()

    def onchannel(self, channel, listener):
        self.channel_listeners[channel] = listener

    def offchannel(self, channel):
        self.channel_listeners.pop(channel, None)


socket = AsyncSocketCluster
