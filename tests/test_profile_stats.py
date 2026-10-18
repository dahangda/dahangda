import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.profile_stats import (
    END, START, HISTORICAL_AUTHOR_EMAILS, GitHub, HTTPError, StatsError,
    collect, collect_repository, replace_stats, update,
)


def sha(number):
    return f"{number:040x}"


def commit(number, author=7):
    return {"sha": sha(number), "author": {"id": author}}


class FixtureAPI(GitHub):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.calls = []

    def get(self, endpoint, **params):
        self.calls.append((endpoint, params))
        if endpoint == "user":
            return {"id": 7, "login": "dahangda"}
        if params.get("author") in HISTORICAL_AUTHOR_EMAILS["dahangda"]:
            items = self.data.get((endpoint, params.get("sha"), params["author"]), [])
        else:
            items = self.data[(endpoint, params.get("sha"))]
        if isinstance(items, Exception):
            raise items
        offset = (params["page"] - 1) * params["per_page"]
        return items[offset:offset + params["per_page"]]


def fixture():
    return FixtureAPI({
        ("user/repos", None): [
            {"id": 1, "full_name": "owner/public", "private": False},
            {"id": 2, "full_name": "org/private", "private": True},
            {"id": 3, "full_name": "owner/fork", "fork": True},
            {"id": 4, "full_name": "owner/empty"},
        ],
        ("repos/owner/public/branches", None): [
            {"name": "main", "commit": {"sha": sha(10)}},
            {"name": "feature/not-merged", "commit": {"sha": sha(11)}},
        ],
        ("repos/org/private/branches", None): [{"commit": {"sha": sha(12)}}],
        ("repos/owner/fork/branches", None): [
            {"commit": {"sha": sha(10)}}, {"commit": {"sha": sha(13)}}],
        ("repos/owner/empty/branches", None): [],
        ("repos/owner/public/commits", sha(10)): [commit(1)],
        ("repos/owner/public/commits", sha(11)): [commit(2), commit(1)],
        ("repos/org/private/commits", sha(12)): [commit(3)],
        ("repos/owner/fork/commits", sha(13)): [commit(4), commit(2), commit(1)],
    })


class CollectionTests(unittest.TestCase):
    def test_feature_private_and_fork_commits_count_once(self):
        api = fixture()
        self.assertEqual(collect(api, "dahangda"), {"commits": 4, "repositories": 4, "branches": 5, "unavailable_repositories": 0})
        requests = [(path, params) for path, params in api.calls if path.endswith("/commits")]
        self.assertEqual(len(requests), 12)  # Three identities; shared fork tip reuses history.
        self.assertEqual({params["author"] for _, params in requests},
                         {"dahangda", *HISTORICAL_AUTHOR_EMAILS["dahangda"]})
        self.assertIn(("repos/owner/public/commits", {"per_page": 100, "page": 1,
                       "sha": sha(11), "author": "dahangda"}), requests)

    def test_historical_emails_include_unlinked_commits_and_deduplicate(self):
        api = fixture()
        company, old_email = HISTORICAL_AUTHOR_EMAILS["dahangda"]
        def historical(number, email, linked=False):
            return {"sha": sha(number), "author": {"id": 7} if linked else None,
                    "commit": {"author": {"name": "mac", "email": email}}}
        api.data[("repos/owner/public/commits", sha(11), company)] = [
            historical(1, company, linked=True), historical(5, company)]
        api.data[("repos/org/private/commits", sha(12), company)] = [historical(5, company)]
        api.data[("repos/owner/fork/commits", sha(13), old_email)] = [historical(6, old_email)]
        self.assertEqual(collect(api, "dahangda")["commits"], 6)

    def test_historical_email_queries_paginate(self):
        api = fixture()
        email = HISTORICAL_AUTHOR_EMAILS["dahangda"][0]
        api.data[("repos/org/private/commits", sha(12), email)] = [
            {"sha": sha(n), "author": None, "commit": {"author": {"email": email}}}
            for n in range(1000, 1101)]
        self.assertEqual(collect(api, "dahangda")["commits"], 105)

    def test_invalid_historical_identity_or_query_failure_preserves_readme(self):
        email = HISTORICAL_AUTHOR_EMAILS["dahangda"][0]
        bad_results = [
            [{"sha": sha(5), "author": None}],
            [{"sha": sha(5), "author": None, "commit": {
                "author": {"name": "yanhang", "email": "someone@example.com"},
                "committer": {"email": email}}}],
            [{"sha": sha(5), "author": {"id": 8},
              "commit": {"author": {"email": email}}}],
            HTTPError(403),
        ]
        for result in bad_results:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as directory:
                api = fixture()
                api.data[("repos/org/private/commits", sha(12), email)] = result
                readme = Path(directory) / "README.md"
                original = START + "previous valid count" + END
                readme.write_text(original)
                with self.assertRaises(StatsError):
                    update(api, "dahangda", readme)
                self.assertEqual(readme.read_text(), original)

    def test_historical_emails_are_not_used_for_another_account(self):
        api = fixture()
        collect_repository(api, "owner/public", "another-user", 7, set())
        self.assertTrue(all(params["author"] == "another-user"
                            for path, params in api.calls if path.endswith("/commits")))

    def test_pagination_for_repositories_branches_and_commits(self):
        repos = [{"id": n, "full_name": f"owner/repo{n}"} for n in range(101)]
        data = {("user/repos", None): repos}
        for repo in repos:
            data[(f"repos/{repo['full_name']}/branches", None)] = []
        data[("repos/owner/repo100/branches", None)] = [
            {"commit": {"sha": sha(n)}} for n in range(1000, 1101)]
        for n in range(1000, 1101):
            data[("repos/owner/repo100/commits", sha(n))] = []
        data[("repos/owner/repo100/commits", sha(1100))] = [commit(n) for n in range(201)]
        self.assertEqual(collect(FixtureAPI(data), "dahangda"),
                         {"commits": 201, "repositories": 101, "branches": 101, "unavailable_repositories": 0})

    def test_wrong_owner_and_unverified_author_fail(self):
        with self.assertRaises(StatsError):
            collect(fixture(), "another-user")
        for author in ({"id": 8}, None):
            api = fixture()
            api.data[("repos/org/private/commits", sha(12))] = [{"sha": sha(3), "author": author}]
            with self.assertRaises(StatsError):
                collect(api, "dahangda")

    def test_failure_after_partial_scan_preserves_readme(self):
        api = fixture()
        api.data[("repos/org/private/commits", sha(12))] = StatsError("Request failed")
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            original = f"profile\n{START}\nprevious valid count\n{END}\nfooter"
            readme.write_text(original)
            with self.assertRaises(StatsError):
                update(api, "dahangda", readme)
            self.assertEqual(readme.read_text(), original)

    def test_blocked_repo_is_excluded_and_recorded(self):
        api = fixture()
        api.data[("repos/org/private/commits", sha(12))] = HTTPError(451)
        result = collect(api, "dahangda")
        self.assertEqual(result["commits"], 3)
        self.assertEqual(result["unavailable_repositories"], 1)
        text = replace_stats(START + END, result["commits"])
        self.assertEqual(text, START + "\n**GitHub 全分支提交 · 3 次**\n" + END)
        for status in (403, 404, 409, 429, 500):
            api = fixture()
            api.data[("repos/org/private/commits", sha(12))] = HTTPError(status)
            with self.assertRaises(HTTPError):
                collect(api, "dahangda")

    def test_blocked_repo_partial_commits_are_discarded(self):
        api = fixture()
        api.data[("repos/org/private/branches", None)].insert(0, {"commit": {"sha": sha(20)}})
        api.data[("repos/org/private/commits", sha(20))] = [commit(99)]
        api.data[("repos/org/private/commits", sha(12))] = HTTPError(451)
        self.assertEqual(collect(api, "dahangda")["commits"], 3)

    def test_dry_run_and_idempotent_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            original = f"profile\n{START}\nold\n{END}\nfooter"
            readme.write_text(original)
            update(fixture(), "dahangda", readme, dry_run=True)
            self.assertEqual(readme.read_text(), original)
            update(fixture(), "dahangda", readme)
            published = readme.read_text()
            self.assertIn("全分支提交 · 4 次", published)
            self.assertTrue(published.startswith(f"profile\n{START}"))
            self.assertTrue(published.endswith(f"{END}\nfooter"))
            self.assertNotIn("org/private", published)
            update(fixture(), "dahangda", readme)
            self.assertEqual(readme.read_text(), published)

    def test_invalid_markers_rejected(self):
        for content in ("no markers", END + START, START + START + END):
            with self.assertRaises(StatsError):
                replace_stats(content, 10)


class TransportTests(unittest.TestCase):
    @patch("scripts.profile_stats.subprocess.run")
    def test_cli_headers_and_query(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, 'HTTP/2.0 200 OK\nContent-Type: application/json\n\n[]', '')
        self.assertEqual(GitHub().get("repos/owner/repo/commits", sha=sha(1), author="dahangda"), [])
        command = run.call_args.args[0]
        self.assertIn("author=dahangda", command)
        self.assertIn(f"sha={sha(1)}", command)

    @patch("scripts.profile_stats.subprocess.run")
    def test_errors_do_not_expose_private_identifiers(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '', 'gh: private-org/private-repo unavailable (HTTP 403)')
        with self.assertRaises(StatsError) as error:
            GitHub().get("user/repos")
        self.assertIn("HTTP 403", str(error.exception))
        self.assertNotIn("private", str(error.exception))

    @patch("scripts.profile_stats.subprocess.run")
    def test_partial_sso_results_are_not_published(self, run):
        run.return_value = subprocess.CompletedProcess([], 0,
            'HTTP/2.0 200 OK\nX-GitHub-SSO: partial-results; organizations=123\n\n[]', '')
        with self.assertRaises(StatsError):
            GitHub().get("user/repos")


if __name__ == "__main__":
    unittest.main()
