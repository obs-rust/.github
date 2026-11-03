"""Validate the exact issues a PR promises to close, independently of assignees."""

import re

from workflow import claims

LABEL = "claim-conflict"
MARKER = "<!-- obs-rust:pr-claim-check -->"
CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+)?#(?P<number>\d+)"
    r"|https://github\.com/(?P<urlrepo>[\w.-]+/[\w.-]+)/issues/(?P<urlnumber>\d+))",
    re.IGNORECASE,
)


def references(repo, body):
    # Examples in fenced code or inline code do not close GitHub issues.
    body = re.sub(r"```.*?```|`[^`\n]*`|<!--.*?-->", "", body or "", flags=re.S)
    return sorted(
        {
            (m["repo"] or m["urlrepo"] or repo, int(m["number"] or m["urlnumber"]))
            for m in CLOSING.finditer(body)
        }
    )


def exempt(files):
    return bool(files) and all(
        p.endswith(".md") or p.startswith(("docs/", "ci/", ".github/", "tests/")) for p in files
    )


def conflicts(gh, repo, pr):
    refs = references(repo, pr.get("body"))
    if not refs or exempt(gh.pr_files(repo, pr["number"])):
        return []
    reasons = []
    author = pr["user"]["login"]
    for target, number in refs:
        ref = f"{target}#{number}"
        try:
            issue = gh.issue(target, number)
        except (LookupError, RuntimeError):
            reasons.append(f"{ref} could not be read; verify the issue reference and access")
            continue
        if issue.closed:
            reasons.append(
                f"{ref} is closed (including duplicates); reference the open issue instead"
            )
        if "ready" not in issue.labels and not claims.is_p0(issue):
            reasons.append(f"{ref} is not ready")
        claim = claims.current_claim(issue)
        if claim is None:
            reasons.append(f"{ref} has no active claim")
        elif not claims.same(claim.user, author):
            reasons.append(f"{ref} is claimed by {claim.user}, not {author}")
        elif not any(
            claims.same(c.author, author)
            and (claims.CLAIM_RE.match(c.first_line()) or claims.TAKEOVER_RE.match(c.first_line()))
            and c.granted
            for c in issue.comments
        ):
            reasons.append(f"{ref} has no accepted Claiming: comment by {author}")
        if issue.open_blockers or issue.labels & {"decision-needed", "needs-investigation"}:
            reasons.append(f"{ref} has unresolved dependencies or investigation/decision work")
    return reasons
