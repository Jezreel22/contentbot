import json
import unittest
import httpx
from providers import Providers


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        if hasattr(self, "provider"):
            await self.provider.close()

    async def test_ai_drafts_use_selected_model_and_platforms(self):
        calls = []
        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"posts": [
                {"platform": "x", "title": "Real idea", "content": "A useful original observation"}
            ]})}}]})
        self.provider = Providers({"OPENAI_API_KEY": "dummy-key", "AI_MODEL": "test-model"}, httpx.MockTransport(handler))
        result = await self.provider.generate({"topic": "Tech", "platforms": ["x"]})
        self.assertEqual(result["source"], "ai")
        self.assertEqual(calls[0]["model"], "test-model")
        self.assertEqual(json.loads(calls[0]["messages"][1]["content"])["platforms"], ["x"])

    async def test_invalid_ai_output_is_rejected(self):
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"posts": []}'}}]})
        self.provider = Providers({"OPENAI_API_KEY": "dummy-key"}, httpx.MockTransport(handler))
        with self.assertRaisesRegex(ValueError, "invalid drafts"):
            await self.provider.generate({"topic": "Tech", "platforms": ["x"]})

    async def test_facebook_text_adapter_sends_correct_page_payload(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"id": "page_post_id"})
        self.provider = Providers({"FACEBOOK_PAGE_ID": "12345", "FACEBOOK_PAGE_ACCESS_TOKEN": "dummy-token"}, httpx.MockTransport(handler))
        remote = await self.provider.publish_request({"platform": "facebook", "content": "Useful football insight"})
        self.assertEqual(remote, "page_post_id")
        self.assertEqual(str(calls[0].url), "https://graph.facebook.com/v22.0/12345/feed")
        self.assertEqual(json.loads(calls[0].content), {"message": "Useful football insight"})

    async def test_provider_error_does_not_expose_token_or_response(self):
        def handler(request):
            return httpx.Response(403, json={"error": "dummy-token is sensitive"})
        self.provider = Providers({"X_ACCESS_TOKEN": "dummy-token"}, httpx.MockTransport(handler))
        with self.assertRaisesRegex(ValueError, "HTTP 403") as raised:
            await self.provider.publish_request({"platform": "x", "content": "Real tip"})
        self.assertNotIn("dummy-token", str(raised.exception))
