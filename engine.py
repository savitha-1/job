"""Evidence-first ranking primitives. Python 3.10+, no dependencies."""
import math
import re
from collections import defaultdict
from urllib.parse import urlsplit

def eligibility_evidence(location, description):
    # Positive eligibility must describe the candidate's allowed work location,
    # not merely mention a country in an unrelated part of the posting.
    location_text = str(location or "").lower()
    text = (location_text + " " + str(description or "").lower())
    excluded = re.search(r"\b(?:us|usa|uk|canada|eu|europe)\s+only\b|must (?:be|reside|live) (?:based )?in (?:the )?(?:us|usa|uk|canada|eu|europe)\b|only applicants? (?:located|based|residing) in", text)
    india_excluded = re.search(r"\b(?:excluding|except|not available in) india\b", text)
    india = re.search(r"\b(?:india|indian)\b", location_text) or re.search(r"\b(?:based|located|residing|living|working) in india\b|\bapplicants? (?:from|in) india\b|\bindia-based candidates\b", text)
    global_remote = re.search(r"\b(?:worldwide|anywhere in the world|globally remote|remote[- ]from[- ]anywhere|work from anywhere)\b", text)
    evidence = []
    for source, subject in (("location", location_text), ("description", str(description or "").lower())):
        india_pattern = r"\b(?:india|indian)\b" if source == "location" else r"\b(?:based|located|residing|living|working) in india\b|\bapplicants? (?:from|in) india\b|\bindia-based candidates\b"
        for label, pattern in (("india_exclusion", r"\b(?:excluding|except|not available in) india\b"), ("regional_exclusion", r"\b(?:us|usa|uk|canada|eu|europe)\s+only\b|must (?:be|reside|live) (?:based )?in (?:the )?(?:us|usa|uk|canada|eu|europe)\b|only applicants? (?:located|based|residing) in"), ("india_allowed", india_pattern), ("worldwide_remote", r"\b(?:worldwide|anywhere in the world|globally remote|remote[- ]from[- ]anywhere|work from anywhere)\b")):
            match = re.search(pattern, subject)
            if match:
                evidence.append({"signal": label, "source": source, "text": match.group(0)})
    if india_excluded:
        return "excluded", evidence
    if excluded:
        return ("unknown" if india or global_remote else "excluded"), evidence
    if india or global_remote:
        return "eligible_signal", evidence
    return "unknown", evidence


def eligibility(location, description):
    return eligibility_evidence(location, description)[0]

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
    return [dict(candidates[k], skill_matches=fit(candidates[k]), eligibility=eligibility(candidates[k].get("location",""),candidates[k].get("description","")), eligibility_evidence=eligibility_evidence(candidates[k].get("location",""),candidates[k].get("description",""))[1]) for k in reciprocal_rank_fusion([relevant,geographic])]
