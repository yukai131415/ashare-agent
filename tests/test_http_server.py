import unittest
from http.server import BaseHTTPRequestHandler
from a_stock_agents.http_server import ExclusiveHTTPServer


class ExclusiveServerTests(unittest.TestCase):
    def test_second_instance_cannot_share_same_port(self):
        first=ExclusiveHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
        try:
            with self.assertRaises(OSError):
                second=ExclusiveHTTPServer(first.server_address,BaseHTTPRequestHandler)
                second.server_close()
        finally:first.server_close()


if __name__=='__main__':unittest.main()
