import logging
import unittest

from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel

from paw_backend.app import create_app
from paw_backend.errors import ApiError

from .support import make_client, make_settings


class Payload(BaseModel):
    count: int


def build_client() -> TestClient:
    app = create_app(make_settings())
    router = APIRouter(prefix="/test")

    @router.post("/payload")
    async def payload(body: Payload) -> Payload:
        return body

    @router.get("/api-error")
    async def api_error():
        raise ApiError(409, "already_exists", "The thing already exists")

    @router.get("/boom")
    async def boom():
        raise RuntimeError("internal detail that must not reach the client")

    app.include_router(router)
    return make_client(app)


class ErrorResponseTest(unittest.TestCase):
    def setUp(self):
        self.client = build_client()

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        body = response.json()
        self.assertEqual(list(body), ["error"])
        self.assertEqual(body["error"]["code"], code)
        self.assertIsInstance(body["error"]["message"], str)
        self.assertEqual(body["error"]["request_id"], response.headers["x-request-id"])
        return body["error"]

    def test_unknown_path_is_a_not_found_error(self):
        response = self.client.get("/api/v1/does-not-exist")
        error = self.assert_error(response, 404, "not_found")
        self.assertEqual(error["message"], "Not Found")

    def test_wrong_method_keeps_the_allow_header(self):
        response = self.client.post("/api/v1/health")
        self.assert_error(response, 405, "method_not_allowed")
        self.assertEqual(response.headers["allow"], "GET")

    def test_validation_error_lists_locations_but_not_submitted_values(self):
        response = self.client.post("/test/payload", json={"count": "sensitive-text"})
        error = self.assert_error(response, 422, "validation_error")
        self.assertEqual(error["details"][0]["loc"], ["body", "count"])
        self.assertEqual(set(error["details"][0]), {"loc", "message", "type"})
        self.assertNotIn("sensitive-text", response.text)

    def test_application_error_uses_its_status_code_and_code(self):
        response = self.client.get("/test/api-error")
        error = self.assert_error(response, 409, "already_exists")
        self.assertEqual(error["message"], "The thing already exists")

    def test_unexpected_exception_is_a_generic_500(self):
        with self.assertLogs("paw_backend.errors", level=logging.ERROR):
            response = self.client.get("/test/boom")
        error = self.assert_error(response, 500, "internal_error")
        self.assertEqual(error["message"], "Internal server error")
        self.assertNotIn("internal detail", response.text)
        # The 500 still passes through the middleware stack.
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_openapi_documents_the_error_model(self):
        schema = self.client.get("/api/v1/openapi.json").json()
        self.assertIn("ErrorResponse", schema["components"]["schemas"])
        responses = schema["paths"]["/api/v1/health"]["get"]["responses"]
        self.assertIn("4XX", responses)
        self.assertNotIn("422", responses)
        self.assertNotIn("HTTPValidationError", schema["components"]["schemas"])

    def test_interactive_docs_ui_is_not_served(self):
        for path in ("/docs", "/redoc"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
