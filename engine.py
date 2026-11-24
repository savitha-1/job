"""Evidence-first ranking primitives. Python 3.10+, no dependencies."""
import math
import re
from collections import defaultdict
from urllib.parse import urlsplit

def eligibility(location, description):
    text = (location + " " + description).lower()
    if re.search(r"\b(?:us|usa|uk|canada|eu|europe)\s+only\b|must (?:be|reside|live) (?:based )?in (?:the )?(?:us|usa|uk|canada|eu|europe)\b", text):
        return "excluded"
    if re.search(r"\b(?:india|worldwide|anywhere in the world|globally remote)\b", location.lower()):
        return "eligible_signal"
    return "unknown"

def reciprocal_rank_fusion(rankings, k=60):
    scores = defaultdict(float)
    for ranking in rankings:
        for i, key in enumerate(dict.fromkeys(ranking), 1):
            scores[key] += 1 / (k+i)
    return sorted(scores, key=lambda key: (-scores[key], key))

def source_priority(successes, trials, total):
    if not 0 <= successes <= trials <= total:
        raise ValueError("invalid counts")
    return float("inf") if trials == 0 else successes/trials + math.sqrt(2*math.log(max(total,1))/trials)

def canonical_key(job):
    return (job["source"], job["board"], str(job["id"]))

def rank(jobs, skills):
    unique = {canonical_key(j): j for j in jobs}
    candidates = {k:j for k,j in unique.items() if eligibility(j.get("location",""),j.get("description","")) != "excluded"}
    def fit(j):
        text = (j.get("title","")+" "+j.get("description","")).lower()
        return sum(bool(re.search(r"(?<!\w)"+re.escape(s.lower())+r"(?!\w)",text)) for s in skills)
    relevant = sorted(candidates, key=lambda k: (-fit(candidates[k]),k))
    geographic = sorted(candidates, key=lambda k: (eligibility(candidates[k].get("location",""),candidates[k].get("description","")) != "eligible_signal",k))
    return [dict(candidates[k], skill_matches=fit(candidates[k]), eligibility=eligibility(candidates[k].get("location",""),candidates[k].get("description",""))) for k in reciprocal_rank_fusion([relevant,geographic])]
