"""Production topology checks that do not contact external services."""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ProductionTopologyTests(unittest.TestCase):
    def test_only_caddy_publishes_ports_and_web_image_contains_frontend(self):
        compose = yaml.safe_load((ROOT / 'compose.production.yaml').read_text(encoding='utf-8'))
        services = compose['services']
        self.assertNotIn('ports', services['api'])
        self.assertNotIn('ports', services['trade-worker'])
        self.assertEqual(services['caddy']['build']['dockerfile'], 'Dockerfile.web')
        self.assertEqual(set(services['caddy']['ports']), {'80:80', '443:443', '443:443/udp'})
        dockerfile = (ROOT / 'Dockerfile.web').read_text(encoding='utf-8')
        self.assertIn('RUN npm ci', dockerfile)
        self.assertIn('RUN npm run build', dockerfile)
        self.assertIn('COPY --from=frontend-build /app/dist /srv', dockerfile)

    def test_single_origin_routes_api_websocket_docs_and_spa(self):
        config = (ROOT / 'Caddyfile.production').read_text(encoding='utf-8')
        for route in ('/api/*', '/ws/*', '/documentation*', '/openapi.json'):
            self.assertIn(route, config)
        self.assertIn('reverse_proxy api:8000', config)
        self.assertIn('try_files {path} /index.html', config)
        environment = (ROOT / '.env.production.example').read_text(encoding='utf-8')
        self.assertIn('APP_DOMAIN=invest.example.com', environment)
        self.assertIn('FRONTEND_ORIGIN=https://invest.example.com', environment)
        self.assertIn('AUTO_TRADE_MODE=disabled', environment)


if __name__ == '__main__':
    unittest.main()
