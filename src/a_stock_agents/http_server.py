"""Do not permit multiple Windows dashboard processes to share a listening port."""
import os
import socket
from http.server import ThreadingHTTPServer


class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address=False
    allow_reuse_port=False

    def server_bind(self):
        if os.name=='nt' and hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()
