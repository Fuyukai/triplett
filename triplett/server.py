"""
Server functions.
"""
import trio
from trio import SocketStream

from triplett.http1 import HTTP11Server


async def serve_over_tcp(ip: str, port: int, cbl):
    """
    Serves an ASGI server over TCP.
    """
    async def _do_the_thing(stream: SocketStream):
        server = HTTP11Server(stream, cbl)
        await server._do_read_loop()
        # if we reach this point we've exited the server loop so we should be able to just close it
        return await stream.aclose()

    await trio.serve_tcp(_do_the_thing, port=port, host=ip)
