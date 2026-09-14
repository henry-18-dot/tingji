import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tingji import remote, storage


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = patch.object(storage, 'DATA', self.root)
        p.start(); self.addCleanup(p.stop)
        self.host = 'pc.example.ts.net'
        self.origin = 'https://' + self.host
        (self.root / 'remote.json').write_text(json.dumps({'url': self.origin, 'ownerLogin': 'owner@example.test'}))

    def test_local_without_forwarding(self):
        self.assertTrue(remote.allowed_request({'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'}, 8765))

    def test_owner_with_exact_https_origin(self):
        self.assertFalse(remote.allowed_request({'Host': self.host, 'Origin': self.origin, 'Tailscale-User-Login': 'owner@example.test'}, 8765))

    def test_rejects_identity_or_origin_bypass(self):
        cases = [
            {'Host':self.host},
            {'Host':self.host,'Tailscale-User-Login':'other@example.test'},
            {'Host':self.host,'Tailscale-User-Login':'owner@example.test','Origin':'https://attacker.test'},
            {'Host':'127.0.0.1:8765','X-Forwarded-For':'100.64.0.1'},
            {'Host':'localhost:8765','Origin':'null'},
            {'Host':'pc.example.ts.net.attacker.test','Tailscale-User-Login':'owner@example.test'},
        ]
        for headers in cases:
            with self.subTest(headers=headers), self.assertRaises(PermissionError): remote.allowed_request(headers, 8765)

    def test_missing_or_malformed_configuration_fails_closed(self):
        for value in [{}, {'url': 'http://pc.example.ts.net', 'ownerLogin':'a'}, {'url':self.origin+'/x','ownerLogin':'a'}]:
            (self.root/'remote.json').write_text(json.dumps(value))
            with self.assertRaises(PermissionError): remote.allowed_request({'Host':self.host,'Tailscale-User-Login':'owner@example.test'},8765)

if __name__ == '__main__': unittest.main()
