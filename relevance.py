"""Explainable field-weighted role and technology retrieval for job text."""
import math
import re
from collections import Counter


TECH_ALIASES = {
    "python": ("python",), "go": ("golang", "go language", "go programming", "go"), "typescript": ("typescript", "ts"),
    "node.js": ("node.js", "nodejs", "node"), "react": ("react", "react.js", "reactjs"),
    "kubernetes": ("kubernetes", "k8s"), "postgresql": ("postgresql", "postgres", "pgsql"),
    "aws": ("aws", "amazon web services"), "devops": ("devops", "site reliability", "sre", "platform engineering"),
    "backend": ("backend", "back-end", "server-side"), "full-stack": ("full-stack", "full stack", "fullstack"),
    "software engineering": ("software engineer", "software engineering", "software developer"),
    "infrastructure": ("infrastructure", "cloud engineer", "platform engineer"),
}
ROLE_PATTERNS = re.compile(r"\b(software|backend|back.end|full.stack|platform|infrastructure|cloud|devops|site reliability|sre|frontend|front.end|data engineer|security engineer)\b", re.I)
TOKEN = re.compile(r"[a-z0-9+#.]+", re.I)


def _tokens(text):
    return TOKEN.findall(str(text or "").casefold())


def score_job(job, skills, *, k1=1.5, b=0.75):
    """Return a query-relative score and matched evidence; title terms weigh 2x."""
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    tags = " ".join(map(str, job.get("tags") or []))
    fields = [(title, 2.0), (tags, 1.5), (description, 1.0)]
    tokens = _tokens(" ".join(value for value, _ in fields))
    counts = Counter(tokens)
    avg_len = max(len(tokens), 1)
    matches, score = [], 0.0
    for skill in skills:
        canonical = str(skill or "").strip().casefold()
        variants = TECH_ALIASES.get(canonical, (canonical,))
        best = 0.0
        evidence = None
        for text, weight in fields:
            low = text.casefold()
            for variant in variants:
                if not variant:
                    continue
                # Avoid matching the ordinary verb "go" in prose. The bare
                # token is accepted only in an explicit technical job title.
                if canonical == "go" and variant == "go":
                    if text != title or not re.search(r"\bgo\s+(?:developer|engineer|programmer)\b", title, re.I):
                        continue
                pattern = re.compile(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", re.I)
                if pattern.search(low):
                    term_count = sum(counts.get(t, 0) for t in _tokens(variant))
                    term_count = max(term_count, 1)
                    tf = term_count * weight
                    best = max(best, tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(_tokens(text)) / avg_len)))
                    evidence = variant
        if evidence:
            matches.append({"skill": str(skill), "matched_as": evidence})
            score += best
    role = bool(ROLE_PATTERNS.search(title))
    # A technical word in an unrelated title (e.g., sales) does not qualify by itself.
    score += 1.0 if role else 0.0
    return {"score": round(score, 5), "matches": matches, "role_relevant": role,
            "relevant": role and (bool(matches) or bool(ROLE_PATTERNS.search(title)))}

