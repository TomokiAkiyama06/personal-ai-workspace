"""The first web visitor must not become the Owner (PAW-021).

The Owner is created only by the server-local command
(``python -m paw_backend.cli owner-setup``). These tests pin that no HTTP route
of the application can create, recover or redeem anything for an Owner yet.
PAW-022 adds the web flow that calls ``OwnerSetupService.redeem``: when it does,
these tests must be updated on purpose (the route needs ``require_capability``
or an entry in the public-route list with its rate-limit note).
"""

import re
import unittest
from pathlib import Path

from .support import make_client
from .test_authz_routes import build_app, route_guards

API_DIR = Path(__file__).resolve().parents[1] / "paw_backend" / "api"
SETUP_WORDS = re.compile(r"owner|setup|recover|register|signup", re.IGNORECASE)
GUESSES = (
    "/api/v1/setup",
    "/api/v1/setup/owner",
    "/api/v1/owner",
    "/api/v1/owners",
    "/api/v1/users",
    "/api/v1/register",
    "/api/v1/signup",
    "/api/v1/recover",
    "/setup",
    "/owner",
    "/register",
)


class NoWebPathTest(unittest.TestCase):
    def test_no_route_of_the_application_deals_with_owner_setup_or_recovery(self):
        paths = sorted(route_guards(build_app()))

        self.assertGreater(len(paths), 3)  # the inventory sees the real routes
        self.assertEqual([p for p in paths if SETUP_WORDS.search(p)], [])

    def test_visiting_a_likely_setup_url_creates_nothing_and_finds_nothing(self):
        client = make_client(build_app())

        for path in GUESSES:
            for method in ("GET", "POST", "PUT", "PATCH"):
                with self.subTest(path=path, method=method):
                    response = client.request(method, path)
                    self.assertEqual(response.status_code, 404)

    def test_the_api_package_does_not_use_the_identity_service(self):
        sources = {path.name: path.read_text() for path in API_DIR.rglob("*.py")}

        self.assertIn("health.py", sources)
        for name, source in sources.items():
            with self.subTest(name):
                self.assertNotIn("paw_backend.identity", source)
                self.assertNotIn("OwnerSetupService", source)


if __name__ == "__main__":
    unittest.main()
