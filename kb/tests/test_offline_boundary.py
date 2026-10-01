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

    def test_remote_dns_resolution_is_scoped_and_revoked(self):
        code = '''
import socket
from unittest.mock import Mock
from fos_rag.offline import install, configure_answer_endpoint, check_connection
resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.4', 443))])
socket.getaddrinfo = resolver
install()
configure_answer_endpoint('https://api.example.com/v1', True)
socket.getaddrinfo('api.example.com', 443)
check_connection(('203.0.113.4', 443))
for host, port in [('other.example.com', 443), ('api.example.com', 80)]:
    try:
        socket.getaddrinfo(host, port)
    except PermissionError:
        pass
    else:
        raise AssertionError('Unapproved DNS allowed')
configure_answer_endpoint('', False)
try:
    check_connection(('203.0.113.4', 443))
except PermissionError:
    pass
else:
    raise AssertionError('Resolved IP remained allowed after revocation')
assert resolver.call_count == 1
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
