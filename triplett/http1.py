import traceback
from io import BytesIO

import h11 as h11
import trio as trio
import urllib.parse as up
from functools import partial
from typing import Any, Awaitable, Callable, Dict

rsig = Callable[[], Awaitable[Dict]]
ssig = Callable[[Dict], Awaitable[None]]
bigsig = Callable[[Dict[str, Any]], Callable[[rsig, ssig], Awaitable[None]]]

"""
Some of the more complex things:

    1) Keep-Alive is a bit of a bastard. In order to handle it, first we store a boolean on the 
       server which tells us if the last request we sent requested keep alive.
"""


class HTTP11Server(object):
    """
    Represents a HTTP/1.1 server.
    """

    def __init__(self, sock: trio.SocketStream, callable: bigsig):
        """
        :param sock: The :class:`trio.SocketStream` this server is listening on.
        """
        self._callable = callable
        self._connection = h11.Connection(h11.SERVER)
        self._sock = sock

        # event queue, because asgi is not very good
        self._event_queue = trio.Queue(capacity=1)

        # used to store the cancel scopes
        self._cancels = set()

        self._keep_alive = None
        self._canceller = None
        self._buffer = BytesIO()

    async def __call__(self, *args, **kwargs):
        return await self._do_read_loop()

    async def _recv_callback(self):
        """
        Callable passed to the ASGI awaitable that consumes from the event queue.
        """
        with trio.open_cancel_scope() as scope:
            self._cancels.add(scope)
            try:
                return await self._event_queue.get()
            finally:
                self._cancels.remove(scope)

    async def _send_callback(self, evt: dict):
        """
        Callable passed to the ASGI awaitable that can be used for sending.
        """
        type_ = evt.get("type")
        if type_ == "http.response.start":
            headers = evt.get("headers", [])
            headers = headers + [(b"Server", b"Triplett"),
                                 (b"X-Powered-By", b"Triplett")]

            response = h11.Response(status_code=evt.get("status", 200),
                                    headers=headers, http_version=b"1.1")
            self._buffer.write(self._connection.send(response))
        elif type_ == "http.response.body":
            data = h11.Data(data=evt.get("body", b''))
            self._buffer.write(self._connection.send(data))
            self._buffer.write(self._connection.send(h11.EndOfMessage()))

            # if it's the end, we need to do a bunch of cleanup
            if evt.get("more_body", None) is False:
                return await self._send_and_cleanup()
        else:
            raise RuntimeError("Unknown type code")

    def _do_shutdown(self):
        """
        Performs a shutdown on the server.
        """
        for scope in self._cancels:
            scope.cancel()

        self._canceller.cancel()

    async def _send_and_cleanup(self):
        """
        Sends any pending data, and peforms cleanup.
        """
        # send all pending data down the line now
        data = self._buffer.getvalue()
        self._buffer = BytesIO()
        await self._sock.send_all(data)
        await self._sock.wait_send_all_might_not_block()

        self._do_shutdown()

    # TODO: Send a 500 error
    async def _safety_wrapper(self, fn, send, receive):
        """
        Safely wraps a function so that it does not bring down the whole server.
        """
        try:
            await fn(send=send, receive=receive)
        except Exception:
            print("Application threw an error!")
            traceback.print_exc()
            self._do_shutdown()
            self._keep_alive = False

    # TODO: Oh god, a lot of things.
    async def _do_read_loop(self):
        """
        The main read loop - reads data from the socket, passes it to h11, then sets up ASGI.
        """
        while True:
            # keepalive is false, so we exit early
            if self._keep_alive is False:
                return

            # first we want to read the headers from the connection
            while True:
                data = await self._sock.receive_some(4096)
                self._connection.receive_data(data)
                event = self._connection.next_event()

                if event != h11.NEED_DATA:
                    break

            if isinstance(event, h11.ConnectionClosed):
                # thanks for blatantly lying, by setting a keep-alive header then closing the
                # connection
                return

            # ok, now we have the initial event
            assert isinstance(event, h11.Request), f"Event was {event}"

            # set keep-alive here for this request
            for header, value in event.headers:
                if header.lower() == b"connection" and value.lower() == b"keep-alive":
                    self._keep_alive = True
                else:
                    self._keep_alive = False

            # we have to do a bit of our own decoding in order to get path and query string
            split = up.urlsplit(event.target)
            path = up.unquote(split.path.decode("utf-8"))
            scope = {
                "type": "http",
                "http_version": event.http_version,
                "method": event.method.decode("utf-8").upper(),
                "path": path,
                "query_string": split.query,
                "headers": event.headers
            }
            application = self._callable(scope)

            # open a nursery so we can spawn the new callable, then continue to feed new data in
            # TODO: If this is cancelled, we need to make sure ot's not cancelled on the queue
            # put call, because that might miss valuable data.
            # Although, the chances of that happening is pretty slim, unless the underlying
            # application doesn't want to receive the data.
            async with trio.open_nursery() as n:
                self._canceller = n.cancel_scope
                cbl = partial(self._safety_wrapper, application,
                              send=self._send_callback, receive=self._recv_callback)
                n.start_soon(cbl)

                async def process_event(ev):
                    if isinstance(ev, h11.Data):
                        d = {
                            "type": "http.response.body",
                            "more_body": True,
                            "body": event.data
                        }
                    elif isinstance(ev, h11.EndOfMessage):
                        d = {
                            "type": "http.response.body",
                            "more_body": False,
                            "body": b""
                        }
                    elif isinstance(ev, h11.ConnectionClosed):
                        return
                    else:
                        pass
                    await self._event_queue.put(d)

                # we may have our end of data already, in which case we don't want to wait any
                # further
                ev = self._connection.next_event()
                if ev != h11.NEED_DATA:
                    await process_event(ev)

                # TODO: What happens if we receive another request during this?
                # open an infinite loop so we can then gather body data
                while True:
                    data = await self._sock.receive_some(4096)
                    self._connection.receive_data(data)
                    event = self._connection.next_event()

                    if event == h11.NEED_DATA:
                        continue

                    await process_event(event)
