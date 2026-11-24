"""Deterministic application form planning. No submission or credential handling."""
import re
from pathlib import Path

RULES = {
    "first_name": r"\b(first|given)\s*name\b",
    "last_name": r"\b(last|family|sur)\s*name\b",
    "full_name": r"\b(full|legal)\s*name\b|^name$",
    "email": r"\b(e-?mail)\b",
    "phone": r"\b(phone|mobile|telephone)\b",
    "linkedin": r"\blinkedin\b",
    "github": r"\bgithub\b",
    "portfolio": r"\b(portfolio|personal website)\b",
    "resume": r"\b(resume|résumé|cv|curriculum vitae)\b",
}
SENSITIVE = re.compile(r"authorization|authorisation|visa|sponsor|citizen|disability|gender|race|ethnic|criminal|consent|agree|certify|signature|salary|assessment|captcha", re.I)

def classify(field):
    label = " ".join(str(field.get(k) or "") for k in ("label","name","placeholder"))
    label = re.sub(r"[_-]", " ", label).strip().lower()
    if SENSITIVE.search(label):
        return None
    matches = [key for key, pattern in RULES.items() if re.search(pattern,label,re.I)]
    if field.get("type") == "file":
        return "resume" if matches == ["resume"] else None
    return matches[0] if len(matches)==1 and matches[0]!="resume" else None

def plan(fields, profile):
    actions, review = [], []
    for field in fields:
        key=classify(field)
        value=profile.get(key) if key else None
        if not value or field.get("type") not in ("text","email","tel","url","file","textarea"):
            if field.get("required") or SENSITIVE.search(str(field.get("label",""))):
                review.append({"id":field["id"],"reason":"unresolved field"})
            continue
        if key=="resume":
            p=Path(value)
            if not p.is_file() or p.suffix.lower() not in (".pdf",".docx"):
                review.append({"id":field["id"],"reason":"invalid resume"})
                continue
        actions.append({"id":field["id"],"operation":"upload" if key=="resume" else "fill","profile_key":key})
    return {"actions":actions,"review":review,"ready":not review,"submission_authorized":False}
