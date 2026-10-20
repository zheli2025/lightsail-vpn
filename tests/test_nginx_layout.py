import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('layout_test',ROOT/'scripts/nginx_layout.py')
layout=importlib.util.module_from_spec(spec); spec.loader.exec_module(layout)

VPN='''
limit_req_zone $binary_remote_addr zone=old_zone:10m rate=10r/m;
server {
    listen 80;
    listen [::]:80;
    server_name _;
    root /var/www/html;
    location = /do-not-output-this-address.yaml {
        if ($arg_token != "do-not-output-this-token") { return 403; }
        try_files $uri =404;
    }
    location /geo/ { alias /var/www/html/geo/; }
    location / { try_files $uri $uri/ =404; }
}
'''


class TestNginxLayout(unittest.TestCase):
    def test_static_vpn_is_candidate_and_values_are_not_disclosed(self):
        report=layout.summarize(VPN)
        self.assertTrue(report['candidate'],report)
        self.assertEqual(report['ports'],[80])
        self.assertEqual(report['routes'],['geo','root','subscription'])
        self.assertNotIn('do-not-output',json.dumps(report))

    def test_reverse_proxy_domain_and_external_include_are_not_candidates(self):
        for extra in ['location / { proxy_pass http://secret-user:secret-pass@127.0.0.1:3000; }',
                      'server_name private-domain.example;',
                      'include /etc/nginx/secret-private-snippet;']:
            text=VPN.replace('root /var/www/html;','root /var/www/html;\n'+extra)
            report=layout.summarize(text)
            self.assertFalse(report['candidate'])
            for secret in ('secret-pass','private-domain.example','secret-private-snippet'):
                self.assertNotIn(secret,json.dumps(report))

    def test_stock_default_without_vpn_routes_is_not_candidate(self):
        self.assertFalse(layout.summarize('server {listen 80; root /var/www/html; location / {try_files $uri =404;} }')['candidate'])

    def test_https_and_normal_trailing_slashes_are_supported(self):
        text=VPN.replace('listen 80;','listen 443 ssl;\nssl_certificate /etc/letsencrypt/live/ip/fullchain.pem;\nssl_certificate_key /etc/letsencrypt/live/ip/privkey.pem;')
        text=text.replace('listen [::]:80;','listen [::]:443 ssl;').replace('root /var/www/html;','root /var/www/html/;')
        report=layout.summarize(text)
        self.assertTrue(report['candidate'],report)
        self.assertEqual(report['ports'],[443])
        self.assertNotIn('privkey',json.dumps(report))

    def test_other_roots_ports_and_routes_are_not_candidates(self):
        for text in [VPN.replace('/var/www/html;','/srv/other;'),
                     VPN.replace('listen 80;','listen 8080;'),
                     VPN.replace('location /geo/','location /admin/')]:
            self.assertFalse(layout.summarize(text)['candidate'])

    def test_inspector_handles_links_without_reading_private_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); enabled=root/'etc/nginx/sites-enabled'; available=root/'etc/nginx/sites-available'
            enabled.mkdir(parents=True); available.mkdir()
            (available/'old-vpn').write_text(VPN)
            (enabled/'old-vpn').symlink_to(available/'old-vpn')
            private=root/'private.key'; private.write_text('private body must never be read')
            (enabled/'bad-link').symlink_to(private)
            original=Path.read_text
            def guarded(path,*a,**kw):
                if path==private: raise AssertionError('read private target')
                return original(path,*a,**kw)
            with patch.object(Path,'read_text',guarded): result=layout.inspect_sites(root)
            by_name={r['name']:r for r in result}
            self.assertTrue(by_name['old-vpn']['candidate'])
            self.assertFalse(by_name['bad-link']['candidate'])
            self.assertNotIn('private body',json.dumps(result))

    def test_invalid_configuration_does_not_disclose_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); enabled=root/'etc/nginx/sites-enabled'; enabled.mkdir(parents=True)
            (enabled/'broken').write_text('server { password "unique-secret-value')
            result=layout.inspect_sites(root)
            self.assertFalse(result[0]['candidate'])
            self.assertNotIn('unique-secret-value',json.dumps(result))
