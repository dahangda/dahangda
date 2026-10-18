#!/usr/bin/env python3
"""Count an author's commits reachable from all accessible GitHub branch heads.

Includes owned, collaborator and organization repositories, forks and private
repositories. Match the GitHub account or owner-confirmed historical author
emails; names alone are not sufficient. Reject conflicting account identities.
Includes merge commits authored by the user; deduplicates SHAs globally. Excludes
deleted branches, tags alone, local branches and company-hosted Git servers.
Only aggregate counts are written to the README and workflow logs.
Requires GitHub CLI; Actions supplies GH_TOKEN only to the collection step.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

START = "<!-- PROFILE-STATS:START -->"
END = "<!-- PROFILE-STATS:END -->"
SHA = re.compile(r"[0-9a-f]{40}")
# Historical identities explicitly confirmed by the profile owner. Keep these
# scoped to that account so --username cannot attribute them to another user.
HISTORICAL_AUTHOR_EMAILS = {
    "dahangda": ("yanhang@bobfintech.com.cn", "yanhang@291gmail.com"),
}


class StatsError(Exception):
    """An error safe to display in a public workflow log."""


class HTTPError(StatsError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub request failed (HTTP {status}); collection incomplete.")


class GitHub:
    def __init__(self):
        self.requests = 0

    def get(self, endpoint, **params):
        # Bound work below the usual PAT limit; never publish a partial count.
        if self.requests >= 4500:
            raise StatsError("API request budget exceeded; collection incomplete.")
        self.requests += 1
        command = ["gh", "api", "--hostname", "github.com", "--include",
                   "--method", "GET", endpoint,
                   "-H", "Accept: application/vnd.github+json",
                   "-H", "X-GitHub-Api-Version: 2022-11-28"]
        for key, value in params.items():
            command.extend(["--raw-field", f"{key}={value}"])
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise StatsError("GitHub CLI unavailable or request timed out.") from None
        if result.returncode:
            status = re.search(r"HTTP (\d{3})", result.stderr)
            if status:
                raise HTTPError(int(status[1]))
            raise StatsError("GitHub request failed; collection incomplete.")
        headers, separator, body = result.stdout.partition("\n\n")
        if not separator:
            raise StatsError("Invalid GitHub response headers.")
        if any(line.lower().startswith("x-github-sso:") and
               "partial-results" in line.lower() for line in headers.splitlines()):
            raise StatsError("Organization SSO returned partial results; authorize the token.")
        try:
            return json.loads(body)
        except ValueError:
            raise StatsError("Invalid GitHub response JSON.") from None

    def pages(self, endpoint, **params):
        page = 1
        while True:
            items = self.get(endpoint, per_page=100, page=page, **params)
            if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                raise StatsError("Invalid GitHub list response.")
            yield from items
            if len(items) < 100:
                return
            page += 1


def commit_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise StatsError("Invalid commit SHA in GitHub response.")
    return value


def collect(api, username):
    user = api.get("user")
    if (not isinstance(user, dict) or
            str(user.get("login", "")).casefold() != username.casefold()):
        raise StatsError("Statistics token must belong to the profile owner.")
    user_id = user.get("id")
    if not isinstance(user_id, int):
        raise StatsError("Missing GitHub author identity.")
    commits, heads, repositories = set(), set(), set()
    branches, skipped = 0, 0
    for repo in api.pages("user/repos", visibility="all",
                          affiliation="owner,collaborator,organization_member",
                          sort="full_name", direction="asc"):
        repo_id, full_name = repo.get("id"), repo.get("full_name")
        if not isinstance(repo_id, int) or not isinstance(full_name, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
            raise StatsError("Invalid repository identity in GitHub response.")
        if repo_id in repositories:
            continue
        repositories.add(repo_id)
        try:
            repo_commits, repo_heads, repo_branches = collect_repository(
                api, full_name, username, user_id, heads)
        except HTTPError as error:
            # GitHub can list legally blocked repositories but reject reading
            # them with 451. Exclude the whole repository, log its count,
            # and do not treat permission, rate-limit or server failures as zero.
            if error.status != 451:
                raise
            skipped += 1
            continue
        commits.update(repo_commits)
        heads.update(repo_heads)
        branches += repo_branches
    return {"commits": len(commits), "repositories": len(repositories) - skipped,
            "branches": branches, "unavailable_repositories": skipped}


def collect_repository(api, full_name, username, user_id, completed_heads):
    commits, heads = set(), set()
    branches = 0
    emails = HISTORICAL_AUTHOR_EMAILS.get(username.casefold(), ())
    for branch in api.pages(f"repos/{full_name}/branches"):
        head = commit_sha((branch.get("commit") or {}).get("sha"))
        branches += 1
        # Equal heads have identical ancestry, even across forks. Immutable
        # SHAs keep moving branch tips from shifting commit pagination.
        if head in completed_heads or head in heads:
            continue
        heads.add(head)
        for identity in (username, *emails):
            for commit in api.pages(f"repos/{full_name}/commits", sha=head, author=identity):
                author = commit.get("author")
                if identity == username:
                    valid = isinstance(author, dict) and author.get("id") == user_id
                else:
                    # Inspect the raw author email, never the committer or name.
                    email = ((commit.get("commit") or {}).get("author") or {}).get("email")
                    valid = (isinstance(email, str) and email.casefold() == identity and
                             (author is None or
                              isinstance(author, dict) and author.get("id") == user_id))
                if not valid:
                    raise StatsError("Author identity could not be verified; collection incomplete.")
                # Account and email queries can overlap, including after an email
                # is linked to GitHub. Deduplicate across queries and repositories.
                commits.add(commit_sha(commit.get("sha")))
    return commits, heads, branches


def replace_stats(content, count):
    if content.count(START) != 1 or content.count(END) != 1 or content.index(START) >= content.index(END):
        raise StatsError("README statistics markers are missing or invalid.")
    before, rest = content.split(START, 1)
    _, after = rest.split(END, 1)
    section = f"\n**GitHub 全分支提交 · {count:,} 次**\n"
    return before + START + section + END + after


def update(api, username, readme, dry_run=False):
    original = readme.read_text(encoding="utf-8")
    replace_stats(original, 0)  # Check markers before any API calls.
    result = collect(api, username)
    content = replace_stats(original, result["commits"])
    # A failed or interrupted scan never touches the previous published count.
    if not dry_run and content != original:
        if readme.read_text(encoding="utf-8") != original:
            raise StatsError("README changed during collection; refusing to overwrite it.")
        readme.write_text(content, encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="dahangda")
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    api = GitHub()
    try:
        result = update(api, args.username, args.readme, dry_run=args.dry_run)
    except (StatsError, OSError) as error:
        # Raw CLI output can include private repository identifiers.
        message = str(error) if isinstance(error, StatsError) else "Unable to read or write README."
        print(f"{message} No statistics update was published.", file=sys.stderr)
        return 1
    print(json.dumps({**result, "requests": api.requests, "dry_run": args.dry_run}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
