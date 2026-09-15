import os
import subprocess
import sys

from django.test import SimpleTestCase, override_settings


class OfflineBoundaryTests(SimpleTestCase):
    def test_process_boundary_in_child_without_disabling_machine_network(self):
        code = '''
from fos_rag.offline import install
import socket
install()
for address in [('example.com', 443), ('203.0.113.1', 443)]:
    try:
        socket.create_connection(address, timeout=0.1)
    except PermissionError:
        pass
    else:
        raise AssertionError('External connection was allowed')
socket.getaddrinfo('127.0.0.1', 8000)
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @override_settings(LOCAL_ONLY=True)
    def test_browser_remote_resources_are_blocked(self):
        from django.http import HttpResponse
        from fos_rag.local_middleware import LocalResourcePolicy
        response = LocalResourcePolicy(lambda request: HttpResponse('ok'))(None)
        self.assertIn("connect-src 'self'", response['Content-Security-Policy'])
        self.assertIn("img-src 'self' data: blob:", response['Content-Security-Policy'])
