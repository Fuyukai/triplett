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
        print("Server died, F")

    await trio.serve_tcp(_do_the_thing, port=port, host=ip)
