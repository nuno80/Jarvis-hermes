"""Unit tests for Todoist integration (Issue #47 / ADR 0016)."""

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from jarvis_hermes.todoist import TodoistClient, TodoistError


class TodoistTests(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.get("TODOIST_API_TOKEN")
        if "TODOIST_API_TOKEN" in os.environ:
            del os.environ["TODOIST_API_TOKEN"]

    def tearDown(self):
        if self.orig_env is not None:
            os.environ["TODOIST_API_TOKEN"] = self.orig_env
        elif "TODOIST_API_TOKEN" in os.environ:
            del os.environ["TODOIST_API_TOKEN"]

    def test_fails_closed_without_token(self):
        """Senza TODOIST_API_TOKEN solleva PROVIDER_NOT_CONFIGURED, mai dati simulati."""
        client = TodoistClient()
        with self.assertRaises(TodoistError) as ctx:
            client.create_task_list(name="Spesa")
        self.assertEqual(ctx.exception.code, "PROVIDER_NOT_CONFIGURED")
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("TODOIST_API_TOKEN", ctx.exception.message)

    def test_fails_without_name_or_tasks(self):
        """Con token ma senza nome né task solleva INVALID_PARAMETER."""
        client = TodoistClient(api_token="valid_token_123")
        with self.assertRaises(TodoistError) as ctx:
            client.create_task_list(name="", tasks=[])
        self.assertEqual(ctx.exception.code, "INVALID_PARAMETER")

    def test_accepts_todoist_api_key_alias(self):
        """Accetta TODOIST_API_KEY se TODOIST_API_TOKEN non è definita."""
        with patch.dict(os.environ, {"TODOIST_API_KEY": "alias_token_xyz"}, clear=True):
            client = TodoistClient()
            self.assertEqual(client.api_token, "alias_token_xyz")

    @patch("urllib.request.urlopen")
    def test_create_project_only(self, mock_urlopen):
        """Crea solo la lista (progetto) senza task."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "id": "proj_999",
            "name": "Lista Spesa",
            "url": "https://todoist.com/showProject?id=proj_999",
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = TodoistClient(api_token="test_secret_token_abc")
        result = client.create_task_list(name="Lista Spesa")

        self.assertEqual(result["project"]["id"], "proj_999")
        self.assertEqual(result["project"]["name"], "Lista Spesa")
        self.assertEqual(result["tasks"], [])
        self.assertEqual(result["tasks_created"], 0)
        self.assertEqual(result["url"], "https://todoist.com/showProject?id=proj_999")

        # Verify request details
        self.assertEqual(mock_urlopen.call_count, 1)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, "https://api.todoist.com/api/v1/projects")
        self.assertEqual(req.headers["Authorization"], "Bearer test_secret_token_abc")
        sent_body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(sent_body, {"name": "Lista Spesa"})

    @patch("urllib.request.urlopen")
    def test_create_project_with_tasks(self, mock_urlopen):
        """Crea progetto e i relativi task associati."""
        proj_resp = MagicMock()
        proj_resp.status = 200
        proj_resp.read.return_value = json.dumps({
            "id": "proj_456",
            "name": "Spesa Settimanale",
            "url": "https://todoist.com/showProject?id=proj_456",
        }).encode("utf-8")
        proj_resp.__enter__.return_value = proj_resp

        task1_resp = MagicMock()
        task1_resp.status = 200
        task1_resp.read.return_value = json.dumps({
            "id": "task_1",
            "content": "Latte",
            "url": "https://todoist.com/showTask?id=task_1",
        }).encode("utf-8")
        task1_resp.__enter__.return_value = task1_resp

        task2_resp = MagicMock()
        task2_resp.status = 200
        task2_resp.read.return_value = json.dumps({
            "id": "task_2",
            "content": "Uova",
            "url": "https://todoist.com/showTask?id=task_2",
        }).encode("utf-8")
        task2_resp.__enter__.return_value = task2_resp

        mock_urlopen.side_effect = [proj_resp, task1_resp, task2_resp]

        client = TodoistClient(api_token="test_token_xyz")
        result = client.create_task_list(
            name="Spesa Settimanale",
            tasks=["Latte", "Uova"],
        )

        self.assertEqual(result["project"]["id"], "proj_456")
        self.assertEqual(result["tasks_created"], 2)
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(result["tasks"][0]["content"], "Latte")
        self.assertEqual(result["tasks"][1]["content"], "Uova")

        # Verify requests
        self.assertEqual(mock_urlopen.call_count, 3)
        # Task 1 check
        req_task1 = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(req_task1.full_url, "https://api.todoist.com/api/v1/tasks")
        body1 = json.loads(req_task1.data.decode("utf-8"))
        self.assertEqual(body1, {"content": "Latte", "project_id": "proj_456"})

    @patch("urllib.request.urlopen")
    def test_create_tasks_in_inbox(self, mock_urlopen):
        """Nome 'inbox' crea task direttamente nell'Inbox senza creare un nuovo progetto."""
        task_resp = MagicMock()
        task_resp.status = 200
        task_resp.read.return_value = json.dumps({
            "id": "task_inbox_1",
            "content": "Chiamare commercialista",
        }).encode("utf-8")
        task_resp.__enter__.return_value = task_resp
        mock_urlopen.return_value = task_resp

        client = TodoistClient(api_token="tok")
        result = client.create_task_list(name="inbox", tasks=["Chiamare commercialista"])

        self.assertEqual(result["project"]["id"], "inbox")
        self.assertEqual(result["tasks_created"], 1)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.todoist.com/api/v1/tasks")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {"content": "Chiamare commercialista"})

    @patch("urllib.request.urlopen")
    def test_create_tasks_with_explicit_project_id(self, mock_urlopen):
        """Con project_id esplicito, usa il progetto esistente."""
        task_resp = MagicMock()
        task_resp.status = 200
        task_resp.read.return_value = json.dumps({"id": "t1", "content": "pane"}).encode("utf-8")
        task_resp.__enter__.return_value = task_resp
        mock_urlopen.return_value = task_resp

        client = TodoistClient(api_token="tok")
        result = client.create_task_list(name="", tasks=["pane"], project_id="custom_project_99")

        self.assertEqual(result["project"]["id"], "custom_project_99")
        self.assertEqual(result["tasks_created"], 1)
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {"content": "pane", "project_id": "custom_project_99"})

    @patch("urllib.request.urlopen")
    def test_401_raises_authentication_failed(self, mock_urlopen):
        """HTTP 401 Unauthorized solleva AUTHENTICATION_FAILED (non retryable)."""
        err = urllib.error.HTTPError(
            url="https://api.todoist.com/api/v1/projects",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error": "Invalid token"}'),
        )
        mock_urlopen.side_effect = err

        client = TodoistClient(api_token="invalid_token")
        with self.assertRaises(TodoistError) as ctx:
            client.create_task_list(name="Spesa")
        self.assertEqual(ctx.exception.code, "AUTHENTICATION_FAILED")
        self.assertFalse(ctx.exception.retryable)

    @patch("urllib.request.urlopen")
    def test_429_raises_rate_limit_exceeded(self, mock_urlopen):
        """HTTP 429 solleva RATE_LIMIT_EXCEEDED (retryable)."""
        err = urllib.error.HTTPError(
            url="https://api.todoist.com/api/v1/projects",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=io.BytesIO(b''),
        )
        mock_urlopen.side_effect = err

        client = TodoistClient(api_token="valid_token")
        with self.assertRaises(TodoistError) as ctx:
            client.create_task_list(name="Spesa")
        self.assertEqual(ctx.exception.code, "RATE_LIMIT_EXCEEDED")
        self.assertTrue(ctx.exception.retryable)

    @patch("urllib.request.urlopen")
    def test_500_raises_provider_error(self, mock_urlopen):
        """HTTP 500 solleva PROVIDER_ERROR (retryable)."""
        err = urllib.error.HTTPError(
            url="https://api.todoist.com/api/v1/projects",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=io.BytesIO(b''),
        )
        mock_urlopen.side_effect = err

        client = TodoistClient(api_token="valid_token")
        with self.assertRaises(TodoistError) as ctx:
            client.create_task_list(name="Spesa")
        self.assertEqual(ctx.exception.code, "PROVIDER_ERROR")
        self.assertTrue(ctx.exception.retryable)

    @patch("urllib.request.urlopen")
    def test_network_timeout_raises_provider_error(self, mock_urlopen):
        """Timeout solleva PROVIDER_ERROR (retryable)."""
        mock_urlopen.side_effect = TimeoutError("Connection timed out")

        client = TodoistClient(api_token="valid_token")
        with self.assertRaises(TodoistError) as ctx:
            client.create_task_list(name="Spesa")
        self.assertEqual(ctx.exception.code, "PROVIDER_ERROR")
        self.assertTrue(ctx.exception.retryable)

    def test_token_redacted_never_leaked(self):
        """Il token non deve mai trapelare nei messaggi di errore."""
        token = "secret_canary_token_999888"
        client = TodoistClient(api_token=token)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            try:
                client.create_task_list(name="Test")
            except TodoistError as exc:
                self.assertNotIn(token, str(exc))
                self.assertNotIn(token, exc.message)


if __name__ == "__main__":
    unittest.main()
